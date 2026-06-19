import { onMount } from 'svelte';
import type { PollStore } from './stores/registry';

/** Start a poll store on mount, stop on unmount. Call ONLY from a .svelte component <script>. */
export function usePoll<T>(store: PollStore<T>): PollStore<T> {
  onMount(() => { store.start(); return () => store.stop(); });
  return store;
}
