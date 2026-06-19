import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

export default defineConfig({
  plugins: [svelte()],
  base: '/dashboard/v2/',
  build: {
    outDir: '../static/dashboard-v2/dist',
    emptyOutDir: true,
  },
  server: { port: 5174 },
});
