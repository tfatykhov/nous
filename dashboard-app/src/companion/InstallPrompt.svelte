<script lang="ts">
  // "Add to home screen" affordance. Everything needed to INSTALL the
  // companion already shipped in F092 Phase 4 — manifest, maskable icons,
  // service worker, apple-touch-icon — but nothing ever told the user, and
  // Chrome's own mini-infobar was removed years ago. So the app was
  // installable and effectively undiscoverable.
  //
  // Two paths, because the platforms genuinely differ:
  //  - Chromium fires `beforeinstallprompt`, which we capture and replay from
  //    a real click (the API requires a user gesture, and the event may fire
  //    before this component mounts — companion.ts stashes it).
  //  - iOS Safari has NO install API at all; the only route is Share → Add to
  //    Home Screen, so there we show instructions rather than a dead button.
  //
  // Hidden once running standalone: an install button inside an installed app
  // is noise, and on iOS it would be permanently unactionable.
  import { onMount } from 'svelte';

  interface InstallEvent extends Event {
    prompt: () => Promise<void>;
    userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>;
  }

  const DISMISS_KEY = 'nous-companion-install-dismissed';
  // A SNOOZE, not a tombstone: the old boolean made one × (or one declined
  // Chromium prompt — dismiss() fires on that too) hide the affordance
  // FOREVER, which read as "the install button got lost". Installing is the
  // durable exit; declining is a "not now".
  const DISMISS_TTL_MS = 14 * 24 * 60 * 60 * 1000;

  let deferred = $state<InstallEvent | null>(null);
  let standalone = $state(true);
  let isIOS = $state(false);
  let dismissed = $state(false);
  let showIOSHelp = $state(false);

  function readDismissed(): boolean {
    try {
      const raw = localStorage.getItem(DISMISS_KEY);
      if (!raw) return false;
      // Legacy '1' (the old permanent tombstone) is treated as EXPIRED so
      // existing users who dismissed once get the affordance back.
      const at = Number(raw);
      return Number.isFinite(at) && Date.now() - at < DISMISS_TTL_MS;
    } catch {
      return false; // private mode — just show it
    }
  }

  onMount(() => {
    // `matchMedia` is optional-chained: jsdom does not implement it, and an
    // install affordance must never be the reason the whole shell fails to
    // mount. Absent => treat as not-standalone and fall back to the iOS flag.
    standalone =
      window.matchMedia?.('(display-mode: standalone)')?.matches === true ||
      // iOS Safari does not implement display-mode; it sets navigator.standalone
      (navigator as unknown as { standalone?: boolean }).standalone === true;
    isIOS =
      /iphone|ipad|ipod/i.test(navigator.userAgent) ||
      // iPadOS 13+ reports as Mac; the touch check disambiguates
      (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    dismissed = readDismissed();

    // The event usually fires before Svelte mounts, so companion.ts parks it
    // on window; take that first, then keep listening in case it fires later.
    const parked = (window as unknown as { __nousInstallEvent?: InstallEvent })
      .__nousInstallEvent;
    if (parked) deferred = parked;

    const onPrompt = (e: Event) => {
      e.preventDefault();
      deferred = e as InstallEvent;
    };
    const onInstalled = () => {
      deferred = null;
      standalone = true;
    };
    window.addEventListener('beforeinstallprompt', onPrompt);
    window.addEventListener('appinstalled', onInstalled);
    return () => {
      window.removeEventListener('beforeinstallprompt', onPrompt);
      window.removeEventListener('appinstalled', onInstalled);
    };
  });

  async function install() {
    if (!deferred) return;
    await deferred.prompt();
    const { outcome } = await deferred.userChoice;
    // The event is single-use: once prompted it cannot be replayed, so drop it
    // either way rather than leaving a button that silently does nothing.
    deferred = null;
    if (outcome === 'dismissed') dismiss();
  }

  function dismiss() {
    dismissed = true;
    showIOSHelp = false;
    try {
      localStorage.setItem(DISMISS_KEY, String(Date.now()));
    } catch {
      /* private mode — it just reappears next visit */
    }
  }

  const visible = $derived(!standalone && !dismissed && (deferred !== null || isIOS));
</script>

{#if visible}
  <div class="install" role="note">
    <span class="msg">Add Nous to your home screen</span>
    {#if deferred}
      <button class="go" onclick={install}>Install</button>
    {:else}
      <button class="go" onclick={() => (showIOSHelp = !showIOSHelp)}>How</button>
    {/if}
    <button class="x" onclick={dismiss} aria-label="dismiss">×</button>
    {#if showIOSHelp}
      <p class="help">
        Tap the Share button, then <strong>Add to Home Screen</strong>.
      </p>
    {/if}
  </div>
{/if}

<style>
  .install {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 0.45rem 0.6rem;
    margin-bottom: 0.6rem;
    font-size: 0.82rem;
  }
  .msg {
    color: var(--text);
    flex: 1;
    min-width: 0;
  }
  .go {
    background: var(--accent);
    color: var(--on-accent);
    border: none;
    border-radius: var(--radius-sm);
    padding: 0.25rem 0.7rem;
    font: inherit;
    font-weight: 600;
    cursor: pointer;
  }
  .go:hover {
    background: var(--accent-dim);
  }
  .x {
    background: none;
    border: none;
    color: var(--muted);
    font-size: 1.1rem;
    line-height: 1;
    padding: 0 0.2rem;
    cursor: pointer;
  }
  .x:hover {
    color: var(--text);
  }
  .help {
    flex-basis: 100%;
    margin: 0.2rem 0 0;
    color: var(--muted);
  }
</style>
