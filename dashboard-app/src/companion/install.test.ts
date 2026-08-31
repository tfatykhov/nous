import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';
import InstallPrompt from './InstallPrompt.svelte';

// "Add to home screen". Everything needed to install shipped in F092 Phase 4;
// what was missing was any affordance. The branching (Chromium event vs iOS
// instructions vs already-installed) is the part that actually breaks, so it
// is what these cover.

function setUA(ua: string, opts: { standalone?: boolean; touch?: number; platform?: string } = {}) {
  Object.defineProperty(navigator, 'userAgent', { value: ua, configurable: true });
  Object.defineProperty(navigator, 'maxTouchPoints', {
    value: opts.touch ?? 0,
    configurable: true,
  });
  Object.defineProperty(navigator, 'platform', {
    value: opts.platform ?? 'Win32',
    configurable: true,
  });
  (navigator as unknown as { standalone?: boolean }).standalone = opts.standalone ?? false;
  window.matchMedia = ((q: string) => ({
    matches: opts.standalone === true && q.includes('standalone'),
    media: q,
    addEventListener() {},
    removeEventListener() {},
  })) as unknown as typeof window.matchMedia;
}

function parkEvent() {
  const ev = new Event('beforeinstallprompt') as Event & {
    prompt: () => Promise<void>;
    userChoice: Promise<{ outcome: string }>;
  };
  ev.prompt = vi.fn().mockResolvedValue(undefined);
  ev.userChoice = Promise.resolve({ outcome: 'accepted' });
  (window as unknown as { __nousInstallEvent?: Event }).__nousInstallEvent = ev;
  return ev;
}

beforeEach(() => {
  localStorage.clear();
  delete (window as unknown as { __nousInstallEvent?: Event }).__nousInstallEvent;
});
afterEach(() => cleanup());

describe('install affordance', () => {
  it('offers a real Install button when the browser supplies the event', async () => {
    setUA('Mozilla/5.0 Chrome/120');
    const ev = parkEvent();
    const { getByText } = render(InstallPrompt);
    await tick();

    await fireEvent.click(getByText('Install'));
    expect(ev.prompt).toHaveBeenCalled();
  });

  it('shows iOS instructions instead of a dead button (no install API there)', async () => {
    setUA('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari');
    const { getByText, container } = render(InstallPrompt);
    await tick();

    expect(container.querySelector('.install')).not.toBeNull();
    await fireEvent.click(getByText('How'));
    expect(container.textContent).toContain('Add to Home Screen');
  });

  it('stays hidden once the app is already installed', async () => {
    setUA('Mozilla/5.0 Chrome/120', { standalone: true });
    parkEvent();
    const { container } = render(InstallPrompt);
    await tick();

    expect(container.querySelector('.install')).toBeNull();
  });

  it('stays hidden on a desktop browser that never offers installation', async () => {
    setUA('Mozilla/5.0 Firefox/121');
    const { container } = render(InstallPrompt);
    await tick();

    expect(container.querySelector('.install')).toBeNull();
  });

  it('remembers a dismissal across mounts', async () => {
    setUA('Mozilla/5.0 Chrome/120');
    parkEvent();
    const first = render(InstallPrompt);
    await tick();
    await fireEvent.click(first.getByLabelText('dismiss'));
    expect(first.container.querySelector('.install')).toBeNull();

    cleanup();
    parkEvent();
    const second = render(InstallPrompt);
    await tick();
    expect(second.container.querySelector('.install')).toBeNull();
  });

  it('detects iPadOS, which reports itself as a Mac', async () => {
    setUA('Mozilla/5.0 (Macintosh; Intel Mac OS X) Safari', {
      platform: 'MacIntel',
      touch: 5,
    });
    const { container } = render(InstallPrompt);
    await tick();

    expect(container.querySelector('.install')).not.toBeNull();
  });
});

describe('install affordance — snooze semantics', () => {
  it('a dismissal EXPIRES after the TTL instead of hiding the button forever', async () => {
    setUA('Mozilla/5.0 Chrome/120');
    // Dismissed 15 days ago — past the 14-day snooze.
    localStorage.setItem(
      'nous-companion-install-dismissed',
      String(Date.now() - 15 * 24 * 60 * 60 * 1000),
    );
    parkEvent();
    const { container } = render(InstallPrompt);
    await tick();
    expect(container.querySelector('.install')).not.toBeNull();
  });

  it('treats the legacy permanent "1" tombstone as expired', async () => {
    // Users who dismissed under the old boolean get the affordance back —
    // that permanence is exactly what read as "the install button got lost".
    setUA('Mozilla/5.0 Chrome/120');
    localStorage.setItem('nous-companion-install-dismissed', '1');
    parkEvent();
    const { container } = render(InstallPrompt);
    await tick();
    expect(container.querySelector('.install')).not.toBeNull();
  });

  it('still snoozes within the TTL', async () => {
    setUA('Mozilla/5.0 Chrome/120');
    localStorage.setItem('nous-companion-install-dismissed', String(Date.now() - 1000));
    parkEvent();
    const { container } = render(InstallPrompt);
    await tick();
    expect(container.querySelector('.install')).toBeNull();
  });
});
