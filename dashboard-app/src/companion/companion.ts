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

export default mount(Companion, { target: document.getElementById('app')! });
