/**
 * "Cameras at a point": the button arms a pick, a map click asks for the
 * meteor height, lists the cameras whose footprint contains the point, and a
 * UTC time opens the Detections footage grid with only those cameras.
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

test('pick a point, list the cameras that see it, open their footage grid', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));  await page.setViewportSize({ width: 1400, height: 1200 });
  await login(page);

  // The fixture cameras have no pointing: aim every test camera at the
  // zenith, so a point right beside its station is in view at every height.
  await page.route('**/api/overview', async route => {
    const resp = await route.fetch();
    const data = await resp.json();
    for (const info of Object.values(data)) {
      for (const cam of info.cameras || []) { cam.az = 0; cam.alt = 90; }
    }
    await route.fulfill({ response: resp, json: data });
  });
  // Keep the grid page quiet once it opens, with the picked night available
  // (catch-all first: the most recently registered route wins).
  await page.route('**/api/detections/**', route => route.fulfill({ json: { date: '20261118', detections: [], events: [] } }));
  await page.route('**/api/detections/nights', route => route.fulfill({ json: ['20261118'] }));
  await page.route('**/api/footage/grid/**', route => route.fulfill({
    json: { date: '20261118', bucket_min: 1, max_window_min: 5, degraded: false, columns: [], rows: [] },
  }));

  await page.goto('/network');
  const dot = page.locator('.ov-marker-dot').first();
  await expect(dot).toBeVisible();

  // Zoom in on the first station so a few pixels beside it is nearly overhead.
  const box = await dot.boundingBox();
  const cx = box.x + box.width / 2;
  const cy = box.y + box.height / 2;
  await page.mouse.move(cx, cy);
  await page.keyboard.down('Control');
  for (let i = 0; i < 10; i++) { await page.mouse.wheel(0, -120); await page.waitForTimeout(60); }
  await page.keyboard.up('Control');
  await page.waitForTimeout(800);
  const zoomed = await dot.boundingBox();

  await page.click('#pick-point-btn');
  await expect(page.locator('#pick-point-btn')).toHaveAttribute('aria-pressed', 'true');
  // Left of the dot: the station name sits to its right.
  await page.mouse.click(zoomed.x - 25, zoomed.y + zoomed.height / 2);

  const popup = page.locator('.leaflet-popup.pp-popup');
  await expect(popup).toBeVisible();
  await expect(popup).toContainText('Meteor height?');
  await expect(page.locator('#pick-point-btn')).toHaveAttribute('aria-pressed', 'false');
  await popup.locator('[data-act=find]').click();

  await expect(popup.locator('.pp-title')).toContainText(/\d+ cameras? see this point at 30.120/);
  await expect(popup.locator('.pp-cam').first()).toBeVisible();
  await expect(popup).toContainText('Geometry only');

  await popup.locator('.pp-date').fill('2026-11-19');
  await popup.locator('.pp-hhmm').fill('04:50');
  await popup.locator('[data-act=view]').click();

  await page.waitForURL(/\/events\?/);
  const url = new URL(page.url());
  expect(url.searchParams.get('tab')).toBe('grid');
  expect(url.searchParams.get('date')).toBe('20261118');
  expect(url.searchParams.get('cams')).toMatch(/^TST\d{3}(,TST\d{3})*$/);
  await expect(page.locator('#grid-cam-filter')).toBeVisible();

  expect(errors).toEqual([]);
});
