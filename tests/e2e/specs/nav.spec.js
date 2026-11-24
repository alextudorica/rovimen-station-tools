/**
 * Shared nav (templates/_nav.html) and the station-page tab race.
 *
 * switchTab is on window from module load, but the station page only knows
 * its station once /api/stations returns. A tab clicked in between used to
 * pushState /station/null/<tab>, which 404s on the next reload (seen from the
 * map on 2026-09-04 and 2026-09-14). The click must now wait for init and
 * open that tab on the real station.
 */
import { test, expect } from '@playwright/test';

const ADMIN_USER = 'admin_test';
const PASSWORD = 'test-e2e-pass';

async function login(page) {
  await page.goto('/login');
  await page.fill('#login-username', ADMIN_USER);
  await page.fill('#login-password', PASSWORD);
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

test('a tab clicked before the station list loads opens on the real station', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await login(page);

  let release;
  const gate = new Promise(res => { release = res; });
  await page.route('**/api/stations', async route => {
    await gate;
    await route.continue();
  });
  await page.goto('/station/test_station_1', { waitUntil: 'domcontentloaded' });
  await page.click('#tab-btn-settings');
  expect(page.url()).not.toContain('/station/null');
  release();

  await page.waitForURL(/\/station\/test_station_1\/settings/, { timeout: 10000 });
  await expect(page.locator('#tab-btn-settings')).toHaveClass(/active/);
  expect(errors).toEqual([]);
});

test('station page nav: Network overview is current, admin tools appear for an admin', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await login(page);
  await page.goto('/station/test_station_1');
  await expect(page.locator('.main-nav a.nav-tab.active')).toHaveText('Network overview');
  await expect(page.locator('.main-nav a.nav-tab.active')).toHaveAttribute('href', '/network');
  await expect(page.locator('.main-nav a[href="/map"]')).toHaveCount(0);
  // About is the last entry and opens the panel
  await expect(page.locator('.main-nav > *').last()).toHaveText('About');
  await expect(page.locator('#nav-tools')).toBeVisible({ timeout: 10000 });
  await page.click('#nav-tools summary');
  for (const id of ['#logo-dd-calibration', '#logo-dd-social', '#logo-dd-admin']) {
    await expect(page.locator(id)).toBeVisible();
  }
  await page.keyboard.press('Escape');
  await expect(page.locator('#logo-dd-admin')).toBeHidden();
  expect(errors).toEqual([]);
});

test('header "Go to station": "/" opens it, typing filters, Enter opens the station', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto('/events');
  await page.keyboard.press('/');
  const input = page.locator('.nav-station-filter');
  await expect(input).toBeFocused();
  await input.fill('beta');
  await expect(page.locator('.nav-station-list a')).toHaveCount(1);
  await input.press('Enter');
  await expect(page).toHaveURL(/\/station\/test_station_2/);
  // It is on the station page too, and closes on Escape.
  await page.click('.nav-station-btn');
  await expect(page.locator('#nav-station-menu')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.locator('#nav-station-menu')).toBeHidden();
  expect(errors).toEqual([]);
});

test('header "Go to station" lists stations by ID and keeps a six-camera label visible', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const cams = codes => codes.map(code => ({ code }));
  await page.route('**/api/stations', route => route.fulfill({
    json: {
      gmnro16: { label: 'Bacău', cameras: cams(['RO001E']) },
      gmnro01: { label: 'Ghirdoveni', cameras: cams(['RO000A', 'RO000B', 'RO001K', 'RO001L', 'GH662S', 'GH662W']) },
      gmnde00: { label: 'Johannismühle', cameras: cams(['DE001B']) },
      gmnro10: { label: 'Roit', cameras: cams(['RO000E']) },
      gmnro02: { label: 'Vaslui', cameras: cams(['RO000M']) },
    },
  }));
  await page.goto('/events');
  await page.click('.nav-station-btn');
  const keys = page.locator('.nav-station-list .ns-key');
  await expect(keys).toHaveText(['gmnde00', 'gmnro01', 'gmnro02', 'gmnro10', 'gmnro16']);
  const label = page.locator('.nav-station-list a').nth(1).locator('span').nth(1);
  await expect(label).toHaveText('Ghirdoveni');
  expect((await label.boundingBox()).width).toBeGreaterThan(40);
  expect(errors).toEqual([]);
});
