import { test, expect } from '@playwright/test';

const ADMIN_USER = 'admin_test';
const PASSWORD = 'test-e2e-pass';

const MOCK_NIGHTS = ['20260601'];
const MOCK_CHUNKS = [
  {
    filename: 'TST001_20260601_213000_color.mkv',
    time: '21:30:00',
    size_mb: 12.5,
    meteor_time: null,
    stack: null,
  },
  {
    filename: 'TST001_20260601_220000_color.mkv',
    time: '22:00:00',
    size_mb: 10.2,
    meteor_time: null,
    stack: null,
  },
];

async function login(page) {
  await page.goto('/login');
  await page.fill('#login-username', ADMIN_USER);
  await page.fill('#login-password', PASSWORD);
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

function interceptStationAPIs(page) {
  page.route('**/api/videodb/nights/**', route =>
    route.fulfill({ json: MOCK_NIGHTS })
  );
  page.route('**/api/videodb/chunks/**', route =>
    route.fulfill({ json: MOCK_CHUNKS })
  );
  page.route('**/api/cached-video/**', route =>
    route.fulfill({
      status: 200,
      contentType: 'video/mp4',
      body: Buffer.alloc(0),
    })
  );
}

test.describe('VDB modal lifecycle', () => {
  test.beforeEach(async ({ page }) => {
    interceptStationAPIs(page);
    await login(page);
  });

  test('VDB tab shows camera buttons and clip grid', async ({ page }) => {
    await page.goto('/station/test_station_1');
    await page.click('#tab-btn-videodb');
    const camBtns = page.locator('#pane-videodb .cam-btn');
    await expect(camBtns.first()).toBeVisible({ timeout: 5000 });
    const count = await camBtns.count();
    expect(count).toBe(2);
  });

  test('no console errors on VDB tab with mock data', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await page.goto('/station/test_station_1');
    await page.click('#tab-btn-videodb');
    await page.waitForTimeout(1000);

    expect(errors).toEqual([]);
  });

  test('clicking a clip opens the modal without errors', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await page.goto('/station/test_station_1');
    await page.click('#tab-btn-videodb');
    await page.waitForTimeout(500);

    // `.vdb-tile` is a class the renderer never emits; behind isVisible() that
    // made this whole assertion silently no-op. The card class is
    // `.vdb-chunk-card`, and the click must land on the thumbnail/placeholder
    // rather than the card centre (a link post-#492) — same targeting as
    // vdb-buttons.spec.js.
    const clip = page.locator('.vdb-chunk-card').first();
    await expect(clip).toBeVisible({ timeout: 5000 });
    await clip.locator('.vdb-chunk-nostack, img').first().click();

    // The clip viewer is the shared VideoModal (.vm-backdrop.open), not a
    // static #vdb-modal element.
    await expect(page.locator('.vm-backdrop.open').first()).toBeVisible({ timeout: 3000 });

    expect(errors).toEqual([]);
  });

  test('closing and reopening modal does not throw', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await page.goto('/station/test_station_1');
    await page.click('#tab-btn-videodb');
    await page.waitForTimeout(500);

    const clips = page.locator('.vdb-chunk-card');
    const backdrop = page.locator('.vm-backdrop.open').first();
    const openTarget = () => clips.first().locator('.vdb-chunk-nostack, img').first();
    await expect(clips.first()).toBeVisible({ timeout: 5000 });

    await openTarget().click();
    await expect(backdrop).toBeVisible({ timeout: 3000 });

    // Close via the modal's own button. Escape is deliberately not used:
    // VideoModal binds no keydown handler (MultiDetModal does), so Escape is a
    // no-op here — a separate gap, not something this test should assert.
    await page.locator('.vm-backdrop.open .vm-close-btn').first().click();
    await expect(page.locator('.vm-backdrop.open')).toHaveCount(0, { timeout: 3000 });

    await openTarget().click();
    await expect(page.locator('.vm-backdrop.open').first()).toBeVisible({ timeout: 3000 });

    expect(errors).toEqual([]);
  });

  test('switching cameras in VDB does not throw', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await page.goto('/station/test_station_1');
    await page.click('#tab-btn-videodb');
    await page.waitForTimeout(500);

    const cam2 = page.locator('#pane-videodb .cam-btn').nth(1);
    if (await cam2.isVisible()) {
      await cam2.click();
      await page.waitForTimeout(500);
    }

    expect(errors).toEqual([]);
  });

  test('switching between all station tabs does not throw', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await page.goto('/station/test_station_1');
    await page.waitForLoadState('domcontentloaded');

    const tabIds = ['tab-btn-videodb', 'tab-btn-fdp', 'tab-btn-archive',
                    'tab-btn-settings', 'tab-btn-rms'];
    for (const id of tabIds) {
      await page.click(`#${id}`);
      await page.waitForTimeout(300);
    }

    expect(errors).toEqual([]);
  });
});
