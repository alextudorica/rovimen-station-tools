/**
 * RMS / Detection and Archive in the station page's new design. Detection:
 * each night's meteor strip and numbers come from its RMS detections, a strip
 * tick opens its clip, and older nights open on request. Archive: the
 * calendar covers the camera's whole archive and a click opens that night.
 * Both fit a phone without sideways scrolling. Station APIs are mocked.
 */
import { test, expect } from '@playwright/test';

const HOST = 'test_station_1';
const CAM = 'TST001';
const NIGHTS = ['20261021', '20261020'];

async function login(page) {
  await page.goto('/login');
  await page.fill('#login-username', 'admin_test');
  await page.fill('#login-password', 'test-e2e-pass');
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

const iso = (date, hms) => `${date.slice(0, 4)}-${date.slice(4, 6)}-${date.slice(6)}T${hms}.000Z`;
const night = date => {
  const next = String(+date + 1);
  const dets = [
    { time_utc: iso(date, '20:15:00'), mag_apparent: 1.5, shower: 'ORI', duration_s: 0.6 },
    { time_utc: iso(next, '02:10:00'), mag_apparent: -1.8, shower: 'ORI', duration_s: 1.2, trajectory: { velocity_geo_km_s: 66.2 } },
    { time_utc: iso(next, '02:40:00'), mag_apparent: 2.4, shower: 'SPO', duration_s: 0.4 },
  ];
  const chunks = [dets[1]].map(d => ({
    filename: `${CAM}_${next}_021000_color.mkv`, time: '02:10:00', size_mb: 3, stack: null, stack_subdir: null,
    locked: true, lock_type: 'detection', meteor_time: d.time_utc, detection_offset_s: null, reencoded: true, source: 'archive',
  }));
  return { dets, chunks };
};

async function mock(page) {
  await page.route('**/api/videodb/rmsnights/**', r => r.fulfill({ json: NIGHTS }));
  await page.route('**/api/videodb/chunks/**', r => r.fulfill({ json: night(new URL(r.request().url()).pathname.split('/').pop()).chunks }));
  await page.route('**/api/videodb/rms-detections/**', r =>
    r.fulfill({ json: { detections: night(new URL(r.request().url()).pathname.split('/').pop()).dets } }));
  await page.route('**/api/rms/plots/**', r => r.fulfill({ json: [] }));
  await page.route('**/api/archive/nights_full/**', r => r.fulfill({ json: [
    { date: '20261021', timelapse: `${CAM}_20261021_timelapse.mp4`, timelapse_stack: null, timelapse_size_mb: null,
      meteors: Array.from({ length: 14 }, (_, i) => ({ filename: `${CAM}_20261022_0${i % 6}1000_color.mkv`, stack: null, stack_subdir: null, detection_offset_s: null, meteor_time: null })) },
    { date: '20261001', timelapse: `${CAM}_20261001_timelapse.mp4`, timelapse_stack: null, timelapse_size_mb: null, meteors: [] },
  ] }));
}

for (const [name, size] of [['desktop', { width: 1280, height: 900 }], ['phone', { width: 375, height: 800 }]]) {
  test(`detection nights and the archive calendar on ${name}`, async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.setViewportSize(size);
    await mock(page);
    await login(page);

    await page.goto(`/station/${HOST}/rms`);
    const open = page.locator(`#rms-night-sec-${HOST}-${CAM}-${NIGHTS[0]}`);
    await expect(open.locator('.dt-stats')).toContainText('Meteors3');
    await expect(open.locator('.dt-stats')).toContainText('-1.8');
    await expect(open.locator('.dt-strip .dt-tick')).toHaveCount(3);
    // the meteor with a clip is a button on the strip
    await expect(open.locator('.dt-strip button.dt-tick')).toHaveCount(1);
    await expect(page.locator(`#rms-night-sec-${HOST}-${CAM}-${NIGHTS[1]}.dt-night-closed`)).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);

    await page.goto(`/station/${HOST}/archive`);
    await expect(page.locator('#arc-title')).toHaveText('14 meteor clips over 2 nights');
    await expect(page.locator('.arc-day[data-date="20261021"]')).toHaveClass(/l4/);
    await expect(page.locator(`#archive-night-sec-${CAM}-20261021 .arc-card`)).toHaveCount(12);   // timelapse + 11
    await page.getByRole('button', { name: 'Show all 14 clips' }).click();
    await expect(page.locator(`#archive-night-sec-${CAM}-20261021 .arc-card`)).toHaveCount(15);
    await page.locator('.arc-day[data-date="20261001"]').click();
    await expect(page.locator('.arc-day[data-date="20261001"]')).toHaveClass(/picked/);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
    expect(errors).toEqual([]);
  });
}
