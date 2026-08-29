/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import tailwindcss from '@tailwindcss/vite';
import { resolve } from 'path';

export default defineConfig({
  plugins: [tailwindcss(), svelte()],
  resolve: {
    alias: {
      $lib: resolve(__dirname, 'src/lib'),
    },
    conditions: ['browser'],
  },
  base: '/dashboard/v2/',
  build: {
    outDir: '../static/dashboard-v2/dist',
    emptyOutDir: true,
    rollupOptions: {
      // F092: setting `input` REPLACES the index.html default — both entries
      // must be listed or the dashboard entry silently disappears.
      input: {
        main: resolve(__dirname, 'index.html'),
        companion: resolve(__dirname, 'companion.html'),
      },
    },
  },
  server: { port: 5174 },
  test: {
    environment: 'jsdom',
    globals: false,
  },
});
