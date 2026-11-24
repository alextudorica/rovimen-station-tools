/**
 * The RMS / Detection tab shows a camera's newest nights while older ones are
 * still loading. It used to wait for every night (~90 per camera, two requests
 * each) before rendering anything, so a camera whose old nights the dashboard
 * had not listed recently stayed on "Loading…" for minutes (gmnro09
 * (Sibiciu), 2026-09-27).
 *
 * Station APIs are mocked with page.route(); every test asserts zero pageerror.
 */
import { test, expect } from '@playwright/test';

const ADMIN_USER = 'admin_test';
const PASSWORD = 'test-e2e-pass';
const HOST = 'test_station_1';
const CAM = 'TST001';

async function login(page) {
  await page.goto('/login');
  await page.fill('#login-username', ADMIN_USER);
  await page.fill('#login-password', PASSWORD);
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

// 40 nights, newest first: 20260809 back to 20260701.
const NIGHTS = Array.from({ length: 40 }, (_, i) => {
  const d = new Date(Date.UTC(2026, 6, 1 + 39 - i));
  return d.toISOString().slice(0, 10).replace(/-/g, '');
});

function clipFor(date) {
  return {
    filename: `${CAM}_${date}_220000_color.mkv`, time: '22:00:00', size_mb: 3.2,
    stack: null, stack_subdir: null, locked: true, lock_type: 'detection',
    meteor_time: null, detection_offset_s: null, reencoded: false, source: 'archive',
  };
}

test.describe('RMS / Detection tab loading', () => {
  let errors;
  test.beforeEach(async ({ page }) => {
    errors = [];
    page.on('pageerror', e => errors.push(e.message));
  });
  test.afterEach(() => { expect(errors).toEqual([]); });

  test('the newest nights show while older ones load, then the whole list', async ({ page }) => {
    let release;
    const gate = new Promise(r => { release = r; });
    await page.route('**/api/videodb/rmsnights/**', route => route.fulfill({ json: NIGHTS }));
    await page.route('**/api/videodb/chunks/**', async route => {
      const date = new URL(route.request().url()).pathname.split('/').pop();
      // Everything past the five newest nights waits: a cold Storage Box.
      if (NIGHTS.indexOf(date) >= 5) await gate;
      await route.fulfill({ json: [clipFor(date)] });
    });
    await page.route('**/api/videodb/rms-detections/**', route =>
      route.fulfill({ json: { detections: [] } }));
    await page.route('**/api/rms/plots/**', route => route.fulfill({ json: [] }));

    await login(page);
    await page.goto(`/station/${HOST}/rms`);
    const cards = page.locator(`#rms-chunks-${HOST}-${CAM} .vdb-chunk-card`);
    await expect(cards.first()).toBeVisible({ timeout: 8000 });
    await expect(page.locator(`#rms-more-${HOST}-${CAM}`)).toContainText('of 40');
    expect(await cards.count()).toBeLessThanOrEqual(5);
    await expect(page.locator(`#rms-count-${HOST}-${CAM}`)).toContainText('of 40 nights');

    release();
    // Every night is listed; the newest is open, the rest closed to their strip.
    const nights = page.locator(`[id^="rms-night-sec-${HOST}-${CAM}-"]`);
    await expect(nights).toHaveCount(40, { timeout: 10000 });
    await expect(page.locator(`#rms-chunks-${HOST}-${CAM} .dt-night-closed`)).toHaveCount(39);
    await expect(page.locator(`#rms-more-${HOST}-${CAM}`)).toHaveCount(0);
    await expect(page.locator(`#rms-count-${HOST}-${CAM}`)).toHaveText('40 nights, 40 clips');
    // Opening a closed night shows its clip
    await page.locator(`#rms-night-sec-${HOST}-${CAM}-${NIGHTS[5]}`).getByRole('button', { name: 'Show 1 clip' }).click();
    await expect(cards).toHaveCount(2);
  });

  test('a plot gallery survives the re-render of a later loading step', async ({ page }) => {
    let release;
    const gate = new Promise(r => { release = r; });
    const newest = NIGHTS[0];
    await page.route('**/api/videodb/rmsnights/**', route => route.fulfill({ json: NIGHTS }));
    await page.route('**/api/videodb/chunks/**', async route => {
      const date = new URL(route.request().url()).pathname.split('/').pop();
      if (NIGHTS.indexOf(date) >= 3) await gate;
      await route.fulfill({ json: [clipFor(date)] });
    });
    await page.route('**/api/videodb/rms-detections/**', route =>
      route.fulfill({ json: { detections: [] } }));
    await page.route('**/api/rms/plots/**', route => {
      const date = new URL(route.request().url()).pathname.split('/').pop();
      route.fulfill({ json: date === newest ? [{ filename: `${CAM}_${newest}_radiants.png`, label: 'Radiants' }] : [] });
    });
    await page.route('**/api/rms/plot_image/**', route =>
      route.fulfill({ status: 200, contentType: 'image/png', body: Buffer.alloc(0) }));

    await login(page);
    await page.goto(`/station/${HOST}/rms`);
    const gallery = page.locator(`#rms-plots-${HOST}-${CAM}-${newest}`);
    await expect(gallery.locator('img').first()).toBeAttached({ timeout: 8000 });
    release();
    await expect(page.locator(`[id^="rms-night-sec-${HOST}-${CAM}-"]`)).toHaveCount(40, { timeout: 10000 });
    await expect(gallery.locator('img').first()).toBeAttached();
  });
});
