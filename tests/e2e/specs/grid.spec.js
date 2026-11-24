import { test, expect } from '@playwright/test';

// Grid tab (events.html "Grid") regression tests for two fixes:
//   1. the all-footage "From" time defaults to now-5min UTC, not a fixed 22:00;
//   2. clicking a station thumbnail opens its clip for ANY station, including
//      the "Other stations" (non-RO/MD/OR) cameras that used to no-op.

const ADMIN_USER = 'admin_test';
const PASSWORD = 'test-e2e-pass';

const GRID_DATE = '20260601';

// One footage cell per test camera. Codes are TST* — outside RO/MD/OR — so the
// grid hides them until the "Other stations" toggle is pressed; that is exactly
// the path that used to leave the click handler dead.
const FOOTAGE_GRID = {
  date: GRID_DATE,
  bucket_min: 1,
  max_window_min: 5,
  degraded: false,
  columns: [
    { cam: 'TST001', host_key: 'test_station_1', station_label: 'Test Station Alpha' },
    { cam: 'TST002', host_key: 'test_station_2', station_label: 'Test Station Beta' },
    // No clips in the window: stays as a dimmed "no footage" row.
    { cam: 'TST003', host_key: 'test_station_3', station_label: 'Test Station Gamma' },
  ],
  rows: [
    {
      bucket_start_min: 22 * 60,
      label: '22:00',
      total: 2,
      station_count: 2,
      has_detection: false,
      cells: {
        TST001: {
          host_key: 'test_station_1',
          count: 1,
          has_detection: false,
          chunks: [{
            host_key: 'test_station_1',
            filename: 'TST001_20260601_220000_color.mkv',
            time: '22:00:00',
            stack: null,
            locked: false,
            meteor_time: null,
            detection_offset_s: 0,
          }],
        },
        TST002: {
          host_key: 'test_station_2',
          count: 1,
          has_detection: false,
          chunks: [{
            host_key: 'test_station_2',
            filename: 'TST002_20260601_220000_color.mkv',
            time: '22:00:00',
            stack: null,
            locked: false,
            meteor_time: null,
            detection_offset_s: 0,
          }],
        },
      },
    },
  ],
};

async function login(page) {
  await page.goto('/login');
  await page.fill('#login-username', ADMIN_USER);
  await page.fill('#login-password', PASSWORD);
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

function interceptGridAPIs(page) {
  // Playwright evaluates the most-recently-registered matching route first, so
  // register the broad detections catch-all BEFORE the specific /nights route
  // (which must return a bare date array for the Night selector to populate).
  page.route('**/api/detections/**', route => route.fulfill({ json: { date: GRID_DATE, detections: [], events: [] } }));
  page.route('**/api/detections/nights', route => route.fulfill({ json: [GRID_DATE] }));
  page.route('**/api/footage/grid/**', route => route.fulfill({ json: FOOTAGE_GRID }));
  page.route('**/api/cached-video/**', route =>
    route.fulfill({ status: 200, contentType: 'video/mp4', body: Buffer.alloc(0) })
  );
  // Platepar fan-out during events init — keep it quiet so no console errors.
  page.route('**/api/platepar/**', route => route.fulfill({ json: {} }));
}

test.describe('Grid tab', () => {
  test.beforeEach(async ({ page }) => {
    interceptGridAPIs(page);
    await login(page);
  });

  test('all-footage "From" defaults to now-5min UTC, never the old 22:00', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    // No capture window, so "now" is never pulled into the night (see below).
    await page.route('**/api/twilight/network/**', route =>
      route.fulfill({ json: { capture_start_min: null, capture_end_min: null, station_count: 0 } }));

    await page.goto('/events');
    await page.click('#tab-btn-grid');

    // gridLoad() fills #grid-from with the clamped default on first load.
    const fromInput = page.locator('#grid-from');
    await expect(fromInput).not.toHaveValue('', { timeout: 5000 });
    const value = await fromInput.inputValue();
    expect(value).toMatch(/^\d{2}:\d{2}$/);

    // Assert it is within a couple of minutes of (now-5) UTC, and specifically
    // not the retired 22:00 constant (unless the wall clock genuinely is ~22:05).
    const now = new Date();
    const expectedMin = ((now.getUTCHours() * 60 + now.getUTCMinutes() - 5) % 1440 + 1440) % 1440;
    const [h, m] = value.split(':').map(Number);
    const gotMin = h * 60 + m;
    const delta = Math.min((gotMin - expectedMin + 1440) % 1440, (expectedMin - gotMin + 1440) % 1440);
    expect(delta).toBeLessThanOrEqual(2);

    expect(errors).toEqual([]);
  });

  test('in daytime the default window is the end of the night, inside the capture window', async ({ page }) => {
    // 10:00 UTC the morning after the night of 2026-06-01, whose footage runs
    // 19:30 → 03:40 UTC: "now minus 5" is outside it, so the Grid shows the
    // night's last 5 minutes.
    await page.clock.setFixedTime(new Date('2026-06-02T10:00:00Z'));
    await page.route('**/api/twilight/network/**', route =>
      route.fulfill({ json: { capture_start_min: 19 * 60 + 30, capture_end_min: 3 * 60 + 40, station_count: 2 } }));
    await page.goto('/events');
    await page.click('#tab-btn-grid');
    await expect(page.locator('#grid-from')).toHaveValue('03:35', { timeout: 5000 });
    await expect(page.locator('#grid-to')).toHaveValue('03:40');

    // a time typed before the window starts is pulled up to it
    await page.fill('#grid-from', '18:00');
    await page.dispatchEvent('#grid-from', 'change');
    await expect(page.locator('#grid-from')).toHaveValue('19:30');
  });

  test('clicking an "Other stations" thumbnail opens the clip modal', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await page.goto('/events');
    await page.click('#tab-btn-grid');

    // TST* cameras are hidden by default (non-RO/MD/OR) — reveal them, then the
    // fixed handler must open the clip when the thumb is clicked.
    await page.click('#grid-toggle-other');

    const thumb = page.locator('#grid-main .grid-thumb').first();
    await expect(thumb).toBeVisible({ timeout: 5000 });
    await thumb.click();

    // openSingleVideo -> shared VideoModal backdrop.
    await expect(page.locator('.vm-backdrop.open').first()).toBeVisible({ timeout: 3000 });

    expect(errors).toEqual([]);
  });

  test('footage rows carry the station ID and keep cameras without clips', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await page.goto('/events');
    await page.click('#tab-btn-grid');
    await page.click('#grid-toggle-other');

    const rows = page.locator('#grid-main tbody tr.grid-row');
    await expect(rows).toHaveCount(3, { timeout: 5000 });
    await expect(rows.nth(0).locator('.grid-age')).toHaveText('test_station_1 · Test Station Alpha');

    const empty = page.locator('#grid-main tbody tr.grid-row-empty');
    await expect(empty).toHaveCount(1);
    await expect(empty.locator('.grid-time')).toContainText('TST003');
    await expect(empty.locator('.grid-nofootage')).toHaveText('no footage');
    await expect(empty.locator('.grid-thumb')).toHaveCount(0);

    expect(errors).toEqual([]);
  });

  test('a map-point link (?cams=) shows only those cameras until "Show all"', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await page.goto(`/events?tab=grid&date=${GRID_DATE}&gfrom=21:58&gto=22:03&cams=TST002,TST003`);

    // The picked cameras show even though TST* are "other" cameras.
    const rows = page.locator('#grid-main tbody tr.grid-row');
    await expect(rows).toHaveCount(2, { timeout: 5000 });
    await expect(rows.nth(0).locator('.grid-time')).toContainText('TST002');
    await expect(rows.nth(1).locator('.grid-time')).toContainText('TST003');
    await expect(page.locator('#grid-cam-filter')).toBeVisible();
    await expect(page.locator('#grid-cam-filter .grid-cam-filter-text')).toHaveText('Showing 2 cameras from a map point');
    await expect(page.locator('#grid-mode')).toHaveValue('footage');
    expect(new URL(page.url()).searchParams.get('cams')).toBe('TST002,TST003');

    // "Show all" clears the filter and drops it from the address bar.
    await page.click('#grid-cam-filter-clear');
    await expect(page.locator('#grid-cam-filter')).toBeHidden();
    await page.click('#grid-toggle-other');
    await expect(rows).toHaveCount(3);
    await expect.poll(() => new URL(page.url()).searchParams.get('cams')).toBeNull();

    expect(errors).toEqual([]);
  });

  test('select mode: pick arbitrary clips and view them together (issue #896)', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await page.goto('/events');
    await page.click('#tab-btn-grid');

    // TST* cams are non-RO/MD/OR — reveal them so there are clips to pick.
    await page.click('#grid-toggle-other');

    const thumbs = page.locator('#grid-main .grid-thumb');
    await expect(thumbs.first()).toBeVisible({ timeout: 5000 });

    // View button is hidden until select mode is on.
    const viewBtn = page.locator('#grid-view-selected');
    await expect(viewBtn).toBeHidden();

    // Enter select mode; the button appears, disabled at zero selected.
    await page.click('#grid-select-toggle');
    await expect(viewBtn).toBeVisible();
    await expect(viewBtn).toBeDisabled();
    await expect(viewBtn).toHaveText('View selected (0)');

    // Clicking two thumbs adds them to the set instead of opening a single clip.
    await thumbs.nth(0).click();
    await expect(page.locator('#grid-main .grid-thumb-selected')).toHaveCount(1);
    await expect(viewBtn).toHaveText('View selected (1)');
    await expect(viewBtn).toBeEnabled();

    await thumbs.nth(1).click();
    await expect(page.locator('#grid-main .grid-thumb-selected')).toHaveCount(2);
    await expect(viewBtn).toHaveText('View selected (2)');

    // No single-clip modal opened while in select mode.
    await expect(page.locator('.vm-backdrop.open')).toHaveCount(0);

    // View selected -> shared MultiDetModal with both clips.
    await viewBtn.click();
    const mdm = page.locator('.mdm-backdrop');
    await expect(mdm).toBeVisible({ timeout: 3000 });
    await expect(page.locator('.mdm-backdrop .mdm-cell')).toHaveCount(2);
    // A hand-built set has no shared detection time — the detection jump button
    // is hidden (showDetection:false).
    await expect(page.locator('.mdm-backdrop .mdm-det-btn')).toBeHidden();

    await page.locator('.mdm-backdrop .mdm-close-btn').click();
    await expect(mdm).toBeHidden();

    // Clear empties the set and disables the view button again.
    await page.click('#grid-clear-selected');
    await expect(page.locator('#grid-main .grid-thumb-selected')).toHaveCount(0);
    await expect(viewBtn).toHaveText('View selected (0)');
    await expect(viewBtn).toBeDisabled();

    expect(errors).toEqual([]);
  });
});
