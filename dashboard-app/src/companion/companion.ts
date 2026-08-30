import { mount } from 'svelte';
import './companion.css';
import Companion from './Companion.svelte';

// F092 Phase 4: PWA service worker — production builds only. The worker
// does runtime caching (immutable assets cache-first, shell + surface
// snapshots network-first for the offline read-only view); registering it
// against the vite dev server would cache unhashed dev modules.
if (import.meta.env.PROD && 'serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker
      .register('/dashboard/v2/companion-sw.js', { scope: '/dashboard/v2/' })
      .catch((err) => console.warn('[companion] service worker registration failed:', err));
  });
}

// `beforeinstallprompt` typically fires BEFORE the Svelte tree mounts, so
// the component would never see it. Park it here and let InstallPrompt pick
// it up; preventDefault stops Chrome's own (long-deprecated) mini-infobar.
window.addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault();
  (window as unknown as { __nousInstallEvent?: Event }).__nousInstallEvent = e;
});

export default mount(Companion, { target: document.getElementById('app')! });
