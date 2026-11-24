import { test, expect } from '@playwright/test';

// The video players in the header's design. On a phone the single-clip player
// shows "Download clip" + "More", and More opens the other actions as a bottom
// sheet; the trackbar is inset so its end handles sit outside the OS edge-swipe
// ("back") zone. The multi-camera player stacks one camera per row on a phone
// and no longer shows an empty "No stack" box under every video.

async function login(page) {
  await page.goto('/login');
  await page.fill('#login-username', 'admin_test');
  await page.fill('#login-password', 'test-e2e-pass');
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

function interceptStationAPIs(page) {
  page.route('**/api/videodb/nights/**', r => r.fulfill({ json: ['20260601'] }));
  page.route('**/api/videodb/chunks/**', r => r.fulfill({ json: [
    { filename: 'TST001_20260601_213000_color.mkv', time: '21:30:00', size_mb: 12.5, meteor_time: null, stack: null },
  ] }));
  page.route('**/api/cached-video/**', r => r.fulfill({ status: 200, contentType: 'video/mp4', body: Buffer.alloc(0) }));
}

async function openClip(page) {
  await page.goto('/station/test_station_1');
  await page.click('#tab-btn-videodb');
  await page.waitForTimeout(1200);
  await page.evaluate(() => window.vdbOpenModal(
    'test_station_1', 'TST001', '20260601',
    'TST001_20260601_213000_color.mkv', '21:30:00', 12.5,
  ));
  const modal = page.locator('.vm-backdrop.open');
  await expect(modal).toBeVisible({ timeout: 3000 });
  return modal;
}

test.describe('single-clip player', () => {
  test.beforeEach(async ({ page }) => {
    interceptStationAPIs(page);
    await login(page);
  });

  test('phone: trackbar clear of the screen edges, actions in a More sheet', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.setViewportSize({ width: 390, height: 844 });
    const modal = await openClip(page);

    const edges = await modal.evaluate(m => {
      const s = m.querySelector('.vm-trackbar-start-handle').getBoundingClientRect();
      const e = m.querySelector('.vm-trackbar-end-handle').getBoundingClientRect();
      return { left: s.left, right: window.innerWidth - e.right };
    });
    expect(edges.left, 'start handle outside the left edge-swipe zone').toBeGreaterThanOrEqual(28);
    expect(edges.right, 'end handle outside the right edge-swipe zone').toBeGreaterThanOrEqual(28);

    const sheet = modal.locator('.vm-act-sheet');
    await expect(modal.locator('.vm-more-btn')).toBeVisible();
    await expect(sheet).toBeHidden();
    await expect(modal.locator('.vm-lock-btn')).toBeHidden();

    await modal.locator('.vm-more-btn').click();
    await expect(sheet).toBeVisible();
    await expect(modal.locator('.vm-lock-btn')).toBeVisible();
    await expect(modal.locator('.vm-more-btn')).toHaveAttribute('aria-expanded', 'true');

    // Escape closes the sheet first, the player stays open
    await page.keyboard.press('Escape');
    await expect(sheet).toBeHidden();
    await expect(modal).toBeVisible();

    // tapping outside the sheet closes it too
    await modal.locator('.vm-more-btn').click();
    await expect(sheet).toBeVisible();
    await page.mouse.click(195, 100);
    await expect(sheet).toBeHidden();

    expect(errors).toEqual([]);
  });

  test('desktop: no More button, the action groups sit inline', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    const modal = await openClip(page);
    await expect(modal.locator('.vm-more-btn')).toBeHidden();
    await expect(modal.locator('.vm-lock-btn')).toBeVisible();
    await expect(modal.locator('.vm-act-group[data-label="Keep"]')).toBeVisible();
  });
});

test('phone: multi-camera player stacks one camera per row, no empty stack boxes', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route('**/api/cached-video/**', r => r.fulfill({ status: 200, contentType: 'video/mp4', body: Buffer.alloc(0) }));
  await login(page);
  await page.goto('/events');
  await page.waitForTimeout(800);
  await page.evaluate(() => window.openEventModal({
    event_time: '2026-06-01T22:00:05', witness_count: 2, station_count: 2,
    witnesses: [
      { host_key: 'test_station_1', station_label: 'Test Station Alpha', cam: 'TST001',
        filename: 'TST001_20260601_220000_color.mkv', meteor_time: '2026-06-01T22:00:05',
        detection_offset_s: 5, stack: null },
      { host_key: 'test_station_2', station_label: 'Test Station Beta', cam: 'TST003',
        filename: 'TST003_20260601_215955_color.mkv', meteor_time: '2026-06-01T22:00:05',
        detection_offset_s: 10, stack: null },
    ],
  }, '20260601'));
  const mdm = page.locator('.mdm-backdrop');
  await expect(mdm).toBeVisible({ timeout: 3000 });

  const cells = mdm.locator('.mdm-cell');
  await expect(cells).toHaveCount(2);
  const [a, b] = [await cells.nth(0).boundingBox(), await cells.nth(1).boundingBox()];
  expect(b.y, 'second camera below the first').toBeGreaterThan(a.y + a.height - 1);
  await expect(mdm.locator('.mdm-stack-img-missing').first()).toBeHidden();
  await expect(page.locator('.fab-stack')).toBeHidden();
  expect(errors).toEqual([]);
});
