import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './specs',
  timeout: 30_000,
  // Every worker shares ONE Flask dev server (see `webServer` below), so worker
  // count is really "concurrent load on that single process". Playwright's
  // default is nproc/2, which on a 22-core dev box means 11 browsers hammering
  // it at once — enough contention to make server-timing-sensitive specs flake.
  // Pin it instead of inheriting the machine's core count.
  workers: process.env.CI ? 2 : 4,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'html' : 'list',
  use: {
    baseURL: 'http://127.0.0.1:5111',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  projects: [
    { name: 'chromium', use: { browserName: 'chromium' } },
  ],
  webServer: {
    command: 'uv run python fixtures/start_test_dashboard.py',
    port: 5111,
    reuseExistingServer: false,
    timeout: 15_000,
    stdout: 'ignore',
    stderr: 'pipe',
  },
});
