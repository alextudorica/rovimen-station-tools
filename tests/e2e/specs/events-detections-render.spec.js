import { test, expect } from '@playwright/test';

// Regression guard: the All Detections list rendered its first 100 cards and
// held the rest behind a sentinel below the last station, so every station
// after the first sat empty under its header until the user scrolled to the
// bottom. All cards must land without any scrolling. A camera with no
// detections is not listed, and the pointing compass filters All Detections
// only (the Multi-camera events tab hides it).

const ADMIN_USER = 'admin_test';
const PASSWORD = 'test-e2e-pass';

const PER_STATION = 80;   // 3 stations x 80 = 240 cards, past the 100-card first batch

function nightPayload() {
  const byStation = {};
  for (const s of [1, 2, 3]) {
    const cam = `RO00T${s}`;
    byStation[`test_station_${s}`] = {
      label: `Test Station ${s}`, online: true,
      cameras: {
        [`RO00E${s}`]: [],
        [cam]: Array.from({ length: PER_STATION }, (_, i) => {
          const t = new Date(Date.UTC(2026, 5, 2, 21, 30, i));
          const hms = t.toISOString().slice(11, 19).replace(/:/g, '');
          return {
            filename: `${cam}_20260602_${hms}_color.mkv`,
            meteor_time: t.toISOString().slice(0, 19),
            detection_offset_s: 5, pending: false,
          };
        }),
      },
    };
  }
  return { date: '20260602', by_station: byStation, events: [] };
}

test('every detection card renders without scrolling to the bottom', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.route('**/api/detections/nights', route => route.fulfill({ json: ['20260602'] }));
  await page.route(/\/api\/detections\/\d{8}$/, route => route.fulfill({ json: nightPayload() }));
  await page.route('**/stack/**', route => route.fulfill({ status: 404, body: '' }));
  await page.goto('/login');
  await page.fill('#login-username', ADMIN_USER);
  await page.fill('#login-password', PASSWORD);
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
  await page.goto('/events');

  await expect(page.locator('#detections-main .ev-card')).toHaveCount(3 * PER_STATION, { timeout: 8000 });
  await expect(page.locator('#detections-main .ev-cards').last().locator('.ev-card')).toHaveCount(PER_STATION);
  expect(await page.evaluate(() => window.scrollY)).toBe(0);
  await expect(page.locator('#detections-main .ev-cam-section')).toHaveCount(3);
  await expect(page.locator('#detections-main')).not.toContainText('RO00E1');

  await expect(page.locator('.ev-compass')).toBeVisible();
  await page.click('#tab-btn-events');
  await expect(page.locator('.ev-compass')).toBeHidden();
  await page.click('#tab-btn-detections');
  await expect(page.locator('.ev-compass')).toBeVisible();
  expect(errors).toEqual([]);
});
