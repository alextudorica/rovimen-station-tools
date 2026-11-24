/**
 * Hovering a station on the overview map shows its camera count and its GMN
 * orbits (last 30 days, all time).
 */
import { test, expect } from '@playwright/test';

const ADMIN_USER = 'admin_test';
const PASSWORD = 'test-e2e-pass';

test('station hover card shows cameras and GMN orbits', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto('/login');
  await page.fill('#login-username', ADMIN_USER);
  await page.fill('#login-password', PASSWORD);
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });

  // Every station gets the same counts, so whichever marker is first matches.
  const keys = Object.keys(await (await page.request.get('/api/overview')).json());
  await page.route('**/api/overview/station_orbits', route => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ ready: true, history_complete: true, computed_at: '2026-10-20T10:00:00+00:00',
      stations: Object.fromEntries(keys.map(k => [k, { orbits_30d: 1234, orbits_all: 56789, first_month: '202304' }])) }),
  }));
  await page.goto('/network');
  const dot = page.locator('.ov-marker-dot').first();
  await expect(dot).toBeVisible();
  await dot.hover();
  const tip = page.locator('.leaflet-tooltip.ov-station-tip');
  await expect(tip).toBeVisible();
  await expect(tip).toContainText(/\d+ cameras?/);
  await expect(tip).toContainText('last 30 days: 1,234');
  await expect(tip).toContainText('all time: 56,789');
  await expect(tip).toContainText('since Apr 2023');
  expect(errors).toEqual([]);
});
