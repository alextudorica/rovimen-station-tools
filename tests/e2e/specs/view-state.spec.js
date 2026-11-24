/**
 * Switching one thing must not reset the others.
 *
 * Operators look at a camera at a given UTC minute, then switch camera,
 * night, tab or station to compare. Every switch used to reset the rest:
 * a camera switch in the Video DB jumped to the newest night and cleared the
 * time window, a night change re-clamped the window to twilight, a station
 * switch lost both, and none of it survived a reload or Back. The view now
 * lives in state.view and the URL (?cam=&date=&from=&to=); these tests pin
 * that down through the real pages.
 */
import { test, expect } from '@playwright/test';

const ADMIN_USER = 'admin_test';
const PASSWORD = 'test-e2e-pass';

// 21:30, 22:00 and 23:15 UTC: a 21:45 -> 22:30 window keeps exactly one.
function chunksFor(cam, date) {
  return ['213000', '220000', '231500'].map(t => ({
    filename: `${cam}_${date}_${t}_color.mkv`,
    time: `${t.slice(0, 2)}:${t.slice(2, 4)}:${t.slice(4, 6)}`,
    size_mb: 10, meteor_time: null, stack: null,
  }));
}
const NIGHTS = { TST001: ['20260602', '20260601'], TST002: ['20260602', '20260601'], TST003: ['20260602', '20260601'] };
// Civil sunset 19:00, sunrise 03:00 UTC.
const TWILIGHT = {
  sunset_min: 1125, civil_sunset_min: 1140, nautical_sunset_min: 1170,
  sunrise_min: 195, civil_sunrise_min: 180, nautical_sunrise_min: 150,
};

async function login(page) {
  await page.goto('/login');
  await page.fill('#login-username', ADMIN_USER);
  await page.fill('#login-password', PASSWORD);
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

async function mockStationApis(page) {
  await page.route('**/api/videodb/nights/**', route => {
    const cam = route.request().url().split('/').pop();
    return route.fulfill({ json: NIGHTS[cam] || [] });
  });
  await page.route('**/api/videodb/chunks/**', route => {
    const parts = new URL(route.request().url()).pathname.split('/');
    const date = parts.pop();
    const cam = parts.pop();
    return route.fulfill({ json: chunksFor(cam, date) });
  });
  await page.route('**/api/twilight/**', route => route.fulfill({ json: TWILIGHT }));
  await page.route('**/api/archive/indexed/**', route => route.fulfill({ json: [] }));
  await page.route('**/api/cached-files/**', route => route.fulfill({ json: [] }));
}

async function setWindow(page, from, to) {
  await page.fill('#vdb-time-start', from);
  await page.dispatchEvent('#vdb-time-start', 'change');
  await page.fill('#vdb-time-end', to);
  await page.dispatchEvent('#vdb-time-end', 'change');
}

const cards = page => page.locator('#vdb-results .vdb-chunk-card');

test.describe('view state survives switches', () => {
  let errors;
  test.beforeEach(async ({ page }) => {
    errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await mockStationApis(page);
    await login(page);
  });
  test.afterEach(() => { expect(errors).toEqual([]); });

  test('camera switch in the Video DB keeps the night and the time window', async ({ page }) => {
    await page.goto('/station/test_station_1/videodb');
    await expect(cards(page)).toHaveCount(3, { timeout: 8000 });

    await page.selectOption('#vdb-night', '20260601');
    await expect(cards(page).first()).toContainText('UTC');
    await setWindow(page, '21:45', '22:30');
    await expect(cards(page)).toHaveCount(1, { timeout: 3000 });

    await page.click('#vdb-camera .cam-btn[data-cam="TST002"]');
    await expect(page.locator('#vdb-camera .cam-btn.active')).toHaveText('TST002');
    await expect(page.locator('#vdb-night')).toHaveValue('20260601');
    await expect(page.locator('#vdb-time-start')).toHaveValue('21:45');
    await expect(page.locator('#vdb-time-end')).toHaveValue('22:30');
    await expect(cards(page)).toHaveCount(1, { timeout: 3000 });
    await expect(cards(page).first()).toContainText('22:00:00');
    await expect(page).toHaveURL(/\/station\/test_station_1\/videodb\?cam=TST002&date=20260601&from=21:45&to=22:30$/);

    // A reload (or a pasted link) reopens the same view.
    await page.reload();
    await expect(page.locator('#vdb-camera .cam-btn.active')).toHaveText('TST002', { timeout: 8000 });
    await expect(page.locator('#vdb-night')).toHaveValue('20260601');
    await expect(page.locator('#vdb-time-start')).toHaveValue('21:45');
    await expect(cards(page)).toHaveCount(1, { timeout: 5000 });

    // "Whole night" hands the window back to the twilight clamp.
    await page.click('#vdb-time-reset');
    await expect(page.locator('#vdb-time-start')).toHaveValue('19:00', { timeout: 3000 });
    await expect(cards(page)).toHaveCount(3);
    await expect(page).toHaveURL(/\?cam=TST002&date=20260601$/);
  });

  // Back (the phone's back gesture) or Close on a clip used to rebuild the
  // Video DB from a URL that still named the camera the page opened with,
  // so the operator landed on another camera at the same time.
  for (const how of ['Back', 'Close']) {
    test(`${how} from a clip stays on the camera it was opened from`, async ({ page }) => {
      // Arrive on a camera and window (an "Open in Video DB" link), then
      // switch camera.
      await page.goto('/station/test_station_1/videodb?cam=TST001&date=20260602&from=21:45&to=22:30');
      await expect(cards(page)).toHaveCount(1, { timeout: 8000 });
      await page.click('#vdb-camera .cam-btn[data-cam="TST002"]');
      await expect(page.locator('#vdb-camera .cam-btn.active')).toHaveText('TST002');
      await expect(cards(page).first()).toContainText('22:00:00');
      await expect(cards(page)).toHaveCount(1, { timeout: 3000 });

      let chunkLoads = 0;
      page.on('request', r => { if (r.url().includes('/api/videodb/chunks/')) chunkLoads++; });
      await cards(page).first().click();
      const modal = page.locator('#vdb-modal-container .vm-backdrop.open');
      await expect(modal).toBeVisible({ timeout: 3000 });
      if (how === 'Back') await page.goBack();
      else await modal.locator('.vm-close-btn').click();
      await expect(modal).toHaveCount(0);

      await page.waitForTimeout(600);
      await expect(page.locator('#vdb-camera .cam-btn.active')).toHaveText('TST002');
      await expect(page.locator('#vdb-time-start')).toHaveValue('21:45');
      await expect(cards(page)).toHaveCount(1);
      await expect(page).toHaveURL(/\/videodb\?cam=TST002&date=20260602&from=21:45&to=22:30$/);
      // Closing the player does not reload the list.
      expect(chunkLoads).toBe(0);
    });
  }

  test('a night change keeps the time window', async ({ page }) => {
    await page.goto('/station/test_station_1/videodb');
    await expect(cards(page)).toHaveCount(3, { timeout: 8000 });
    await setWindow(page, '21:45', '22:30');
    await expect(cards(page)).toHaveCount(1, { timeout: 3000 });
    await page.selectOption('#vdb-night', '20260601');
    // Give the (mocked) twilight clamp time to land; it must not win.
    await page.waitForTimeout(500);
    await expect(page.locator('#vdb-time-start')).toHaveValue('21:45');
    await expect(cards(page)).toHaveCount(1);
  });

  test('station switch keeps night and window, and Back restores the first station', async ({ page }) => {
    await page.goto('/station/test_station_1/videodb?date=20260601&from=21:45&to=22:30');
    await expect(cards(page)).toHaveCount(1, { timeout: 8000 });

    await page.click('#station-next-btn');
    await expect(page).toHaveURL(/\/station\/test_station_2\/videodb\?date=20260601&from=21:45&to=22:30$/);
    await expect(page.locator('#vdb-camera .cam-btn.active')).toHaveText('TST003', { timeout: 8000 });
    await expect(page.locator('#vdb-night')).toHaveValue('20260601');
    await expect(page.locator('#vdb-time-start')).toHaveValue('21:45');
    await expect(cards(page)).toHaveCount(1, { timeout: 5000 });

    // Back used to put station 1 in the dropdown over station 2's content.
    await page.goBack();
    await expect(page.locator('#station-dd-current')).toHaveText('test_station_1');
    await expect(page.locator('#vdb-camera .cam-btn')).toHaveText(['TST001', 'TST002'], { timeout: 8000 });
    await expect(page.locator('#vdb-time-start')).toHaveValue('21:45');
  });

  test('a camera picked in the Video DB is the camera on the RMS tab and back', async ({ page }) => {
    await page.goto('/station/test_station_1/videodb');
    await expect(cards(page)).toHaveCount(3, { timeout: 8000 });
    await page.click('#vdb-camera .cam-btn[data-cam="TST002"]');
    await expect(page.locator('#vdb-camera .cam-btn.active')).toHaveText('TST002');

    await page.click('#tab-btn-rms');
    await expect(page.locator('#pane-rms .rms-cam-seg [aria-pressed="true"]')).toHaveText('TST002', { timeout: 5000 });
    await expect(page).toHaveURL(/\/station\/test_station_1\/rms\?cam=TST002/);

    await page.click('#tab-btn-videodb');
    await expect(page.locator('#vdb-camera .cam-btn.active')).toHaveText('TST002');
    await expect(cards(page)).toHaveCount(3, { timeout: 5000 });
  });
});

test.describe('Detections page view state', () => {
  test('a typed time window survives a night change and lands in the URL', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/api/detections/nights', route => route.fulfill({ json: ['20260602', '20260601'] }));
    await page.route(/\/api\/detections\/\d{8}$/, route => {
      const date = route.request().url().split('/').pop();
      return route.fulfill({ json: { date, correlation_window_s: 1, by_station: {}, events: [], detections: [] } });
    });
    await page.route('**/api/twilight/**', route => route.fulfill({ json: TWILIGHT }));
    await login(page);
    await page.goto('/events');
    await expect(page.locator('#ev-date')).toHaveValue('20260602', { timeout: 8000 });
    await expect(page.locator('#ev-time-start')).toHaveValue('19:00', { timeout: 5000 });

    await page.fill('#ev-time-start', '21:45');
    await page.dispatchEvent('#ev-time-start', 'change');
    await page.selectOption('#ev-date', '20260601');
    await page.waitForTimeout(500);
    await expect(page.locator('#ev-time-start')).toHaveValue('21:45');
    // the slider runs to 30 min past civil sunrise (03:00 + 30), the end of RMS capture
    await expect(page).toHaveURL(/\/events\?date=20260601&from=21:45&to=03:30$/);

    await page.reload();
    await expect(page.locator('#ev-date')).toHaveValue('20260601', { timeout: 8000 });
    await expect(page.locator('#ev-time-start')).toHaveValue('21:45');
    expect(errors).toEqual([]);
  });
});

test.describe('map', () => {
  test('clicking a station name opens the station', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await login(page);
    await page.goto('/network');
    const name = page.locator('.ov-marker-name', { hasText: 'Test Station Alpha' });
    await expect(name).toBeVisible({ timeout: 10000 });
    await expect(name.locator('.ov-marker-id')).toHaveText('test_station_1');
    await name.click();
    await page.waitForURL(/\/station\/test_station_1$/, { timeout: 5000 });
    expect(errors).toEqual([]);
  });

  test('clicking a station dot toggles its fields of view in place', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await login(page);
    await page.goto('/network');
    const dot = page.locator('.ov-marker-dot').first();
    await expect(dot).toBeVisible({ timeout: 10000 });
    await dot.click();
    await expect(dot).toHaveClass(/ov-marker-selected/);
    await expect(page.locator('#station-band')).toBeVisible();
    await expect(page).toHaveURL(/\/network$/);
    await dot.click();
    await expect(dot).not.toHaveClass(/ov-marker-selected/);
    await expect(page.locator('#station-band')).toBeHidden();
    expect(errors).toEqual([]);
  });
});
