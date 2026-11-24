import { test, expect } from '@playwright/test';

// Regression guard: an Events-page multi-station event whose second witness has
// no clip in the archive (pending, as every gmnro05 (Bistrița) witness is while
// its meteor clips are encoded on demand only) opened on one camera, and its
// "joint clip" was that one camera. Each witness now gets a tile, and the joint
// clip is offered only when it would join two cameras or more.

const ADMIN_USER = 'admin_test';
const PASSWORD = 'test-e2e-pass';

async function login(page) {
  await page.goto('/login');
  await page.fill('#login-username', ADMIN_USER);
  await page.fill('#login-password', PASSWORD);
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

const witness = (cam, extra) => ({
  host_key: 'test_station_1', station_label: 'Test Station 1', cam,
  meteor_time: '2026-06-01T21:30:05', detection_offset_s: 5,
  stack: `${cam}_20260601_213000_stack.webp`,
  pending: false, no_clip: false, pruned: false,
  ...extra,
});

async function openEvent(page, witnesses) {
  await page.evaluate(ws => {
    window.openEventModal({
      event_time: '2026-06-01T21:30:05', witness_count: ws.length, witnesses: ws,
    }, '20260601');
  }, witnesses);
  await expect(page.locator('.mdm-backdrop')).toBeVisible({ timeout: 3000 });
}

test.describe('events modal: witnesses without a clip', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('**/api/cached-video/**', route =>
      route.fulfill({ status: 200, contentType: 'video/mp4', body: Buffer.alloc(0) }));
    await page.route('**/stack/**', route => route.fulfill({ status: 404, body: '' }));
    await login(page);
    await page.goto('/events');
    await page.waitForLoadState('domcontentloaded');
    await expect(page.locator('#nav-tools')).toBeVisible({ timeout: 10000 });
  });

  test('a pending witness keeps its tile and the joint clip is not offered', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await openEvent(page, [
      witness('TST001', { filename: 'TST001_20260601_213000_color.mkv' }),
      witness('TST002', { filename: null, pending: true }),
    ]);

    const cells = page.locator('.mdm-backdrop .mdm-cell');
    await expect(cells).toHaveCount(2);
    const noclip = page.locator('.mdm-backdrop .mdm-cell-noclip');
    await expect(noclip).toHaveCount(1);
    await expect(noclip.locator('.mdm-cap')).toContainText('TST002');
    await expect(noclip.locator('.mdm-err-msg')).toHaveText('not uploaded yet');
    await expect(page.locator('.mdm-backdrop .mdm-stitch-btn')).toBeHidden();

    expect(errors).toEqual([]);
  });

  test('the joint clip carries only the cameras that have a clip', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    let payload = null;
    await page.route('**/api/stitch-multi', route => {
      payload = route.request().postDataJSON();
      route.fulfill({ status: 200, contentType: 'video/mp4', body: Buffer.from([0, 0, 0, 0]) });
    });

    await openEvent(page, [
      witness('TST001', { filename: 'TST001_20260601_213000_color.mkv' }),
      witness('TST002', { filename: null, pending: true }),
      witness('TST003', { host_key: 'test_station_2', filename: 'TST003_20260601_213000_color.mkv' }),
    ]);

    const stitch = page.locator('.mdm-backdrop .mdm-stitch-btn');
    await expect(stitch).toBeVisible();
    await stitch.click();
    await expect.poll(() => payload, { timeout: 5000 }).not.toBeNull();
    expect(payload.videos.map(v => v.cam)).toEqual(['TST001', 'TST003']);

    expect(errors).toEqual([]);
  });
});
