/**
 * A clip the server is still fetching from the station must read "Fetching",
 * not "Clip not available".
 *
 * Station pulls used to run inside the request and die at nginx's 30 s limit
 * (504). The server now answers 202 + Retry-After while a background job
 * downloads the clip, and the video modal asks the server what an error was
 * (retryVideoIfPreparing) and reloads when told to wait.
 */
import { test, expect } from '@playwright/test';

const ADMIN_USER = 'admin_test';
const PASSWORD = 'test-e2e-pass';

const MOCK_NIGHTS = ['20260601'];
const MOCK_CHUNKS = [
  { filename: 'TST001_20260601_213000_color.mkv', time: '21:30:00', size_mb: 12.5, meteor_time: null, stack: null },
];

async function login(page) {
  await page.goto('/login');
  await page.fill('#login-username', ADMIN_USER);
  await page.fill('#login-password', PASSWORD);
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

test('a clip still being fetched from the station shows "Fetching" and is reloaded', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));

  // Every request for the clip (GET by <video>, HEAD probes) answers
  // "preparing", as it would while the station renders the overlay.
  const hits = { get: 0, head: 0 };
  await page.route('**/api/videodb/nights/**', route => route.fulfill({ json: MOCK_NIGHTS }));
  await page.route('**/api/videodb/chunks/**', route => route.fulfill({ json: MOCK_CHUNKS }));
  await page.route('**/api/archive/indexed/**', route => route.fulfill({ json: [] }));
  await page.route('**/api/cached-files/**', route => route.fulfill({ json: [] }));
  await page.route('**/api/cached-video/**', route => {
    const method = route.request().method();
    if (method === 'HEAD') hits.head += 1; else hits.get += 1;
    return route.fulfill({
      status: 202,
      headers: { 'Retry-After': '1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ preparing: true, detail: 'Fetching the clip from the station' }),
    });
  });

  await login(page);
  await page.goto('/station/test_station_1/videodb');
  const card = page.locator('#vdb-results .vdb-chunk-card').first();
  await expect(card).toBeVisible({ timeout: 8000 });
  await card.click();

  const modal = page.locator('#vdb-modal-container');
  await expect(modal.locator('.vm-video-loading')).toHaveText('Fetching the clip from the station…', { timeout: 8000 });
  await expect(modal.locator('.vm-video-err')).toBeHidden();
  // The player asked again after Retry-After.
  await expect.poll(() => hits.get, { timeout: 8000 }).toBeGreaterThan(1);
  expect(hits.head).toBeGreaterThan(0);
  expect(errors).toEqual([]);
});
