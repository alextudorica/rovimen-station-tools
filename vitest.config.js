import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  resolve: {
    // Pages that import '/static/dashboard-common.js' (the browser maps it
    // through the page's import map) load from the repo under test.
    alias: [{ find: /^\/static\//, replacement: fileURLToPath(new URL('./dashboard/static/', import.meta.url)) }],
  },
  test: {
    include: ['tests/js/**/*.test.js'],
    environment: 'jsdom',
    setupFiles: ['tests/js/setup.js'],
  },
});
