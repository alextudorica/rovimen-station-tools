import { test, expect } from '@playwright/test';

// Regression guard: every detection clip modal must expose an always-available
// "Download stack" button for the BW FF/max-pixel meteor stack, derived from the
// clip identity even when the caller passes no explicit stack URL. Previously the
// events/archive modal had no stack button at all — only the VDB modal wired one
// via extra.stackUrl. The inline preview uses the fast /stack thumbnail, but the
// DOWNLOAD button hands out the branded full-resolution /fullstack image (the
// ROVIMEN/astromania overlay is burned in on demand), so no un-branded stack is
// downloadable. These tests open the events single modal and the archive modal,
// assert the button is present, and assert its href hits /fullstack. Every test
// asserts zero pageerror.

const ADMIN_USER = 'admin_test';
const PASSWORD = 'test-e2e-pass';

// A 1x1 webp so the browser can decode the intercepted stack response.
const FAKE_WEBP = Buffer.from(
  'UklGRhIAAABXRUJQVlA4TAYAAAAvAAAAAA==', 'base64');

async function login(page) {
  await page.goto('/login');
  await page.fill('#login-username', ADMIN_USER);
  await page.fill('#login-password', PASSWORD);
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'),
    { timeout: 10000, waitUntil: 'commit' });
}

// VideoModal decides ONCE, at open(), whether to show its operator-only actions
// (Download stack, Download clip, Crop): `_applyAnonControlGate()` hides them
// when `state.AUTH_USER` is still null, and nothing re-runs the gate when
// /api/auth/status finally lands. `domcontentloaded` fires long before that
// fetch resolves, so driving the modal straight after a goto races it and
// silently yields a public-visitor modal with no stack button at all. Wait for
// the fetch's DOM effect instead -- the admin nav entry, which every page's
// auth init unhides for an admin session.
async function waitForAuthApplied(page) {
  await expect(page.locator('#nav-tools')).toBeVisible({ timeout: 10000 });
}

test.describe('always-available Download-stack button', () => {
  test('events single modal exposes Download stack pointing at /fullstack/', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    // Capture the branded stack request the DOWNLOAD button href points at
    // (fired on click). The inline preview img still uses /stack (fulfilled too).
    let stackUrl = null;
    await page.route('**/fullstack/**', route => {
      stackUrl = route.request().url();
      route.fulfill({ status: 200, contentType: 'image/webp', body: FAKE_WEBP });
    });
    await page.route('**/stack/**', route =>
      route.fulfill({ status: 200, contentType: 'image/webp', body: FAKE_WEBP })
    );
    // Quiet the modal's playback fetch.
    await page.route('**/api/cached-video/**', route =>
      route.fulfill({ status: 200, contentType: 'video/mp4', body: Buffer.alloc(0) })
    );

    await login(page);
    await page.goto('/events');
    await page.waitForLoadState('domcontentloaded');
    await waitForAuthApplied(page);

    // Open the single-clip modal directly — no explicit stack URL is passed by
    // this entry point, so the button must come from the modal's derivation.
    await page.evaluate(() => {
      window.openSingleVideo(
        'test_station_1', 'TST001', '20260601',
        'TST001_20260601_213000_color.mkv', 2.0, null
      );
    });
    const backdrop = page.locator('.vm-backdrop.open').first();
    await expect(backdrop).toBeVisible({ timeout: 3000 });

    const stackDl = backdrop.locator('.vm-stack-dl-btn');
    await expect(stackDl).toBeVisible();

    // The download href is the derived BW FF stack on the branded /fullstack/
    // route (the inline preview img still uses /stack/).
    const href = await stackDl.getAttribute('href');
    expect(href).toBe(
      '/fullstack/test_station_1/TST001/20260601/TST001_20260601_213000_stack.webp');
    expect(await stackDl.getAttribute('download'))
      .toBe('TST001_20260601_213000_stack.webp');
    const previewSrc = await backdrop.locator('.vm-stack-img').getAttribute('src');
    expect(previewSrc).toContain(
      '/stack/test_station_1/TST001/20260601/TST001_20260601_213000_stack.webp');

    // Clicking the native <a download> fires a browser download of the branded
    // /fullstack/ URL. Assert via the download event (the request bypasses
    // page.route for anchor downloads, so we read the resolved URL off it).
    //
    // This is the one assertion in the file that waits on the REAL dashboard:
    // an anchor download can't be intercepted, so Chromium only raises the
    // event once /fullstack has produced response headers. That route walks the
    // archive behind station_client's storage-box concurrency guard, which
    // SHEDS on contention (a 2 s acquire timeout that returns the caller's
    // default) and then falls through to a station fetch and a thumbnail
    // fallback. Under parallel workers that path is variable-latency, so the
    // budget here has to cover a slow answer — the claim under test is which
    // URL the browser resolves, not how fast the server answers it. The
    // fixture also lifts the concurrency cap (no real mount in E2E), so this
    // headroom should stay unused.
    test.setTimeout(60_000);
    const download = await Promise.all([
      page.waitForEvent('download', { timeout: 20_000 }),
      stackDl.click(),
    ]).then(([dl]) => dl);
    expect(download.url()).toContain(
      '/fullstack/test_station_1/TST001/20260601/TST001_20260601_213000_stack.webp');
    void stackUrl;

    expect(errors).toEqual([]);
  });

  test('archive modal exposes Download stack for a detection clip', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    // Archive night with one detection meteor and NO stack recorded in the
    // payload (stack: null), so the modal's Download-stack button must be
    // DERIVED from the clip identity rather than passed in.
    await page.route('**/api/archive/nights_full/**', route => route.fulfill({
      json: [{
        date: '20260601',
        meteors: [{
          filename: 'TST001_20260601_213000_color.mkv',
          stack: null, stack_subdir: null,
          detection_offset_s: 2.0, meteor_time: null,
        }],
        timelapse: null, timelapse_size_mb: null, timelapse_stack: null,
      }],
    }));
    await page.route('**/api/archive/file/**', route =>
      route.fulfill({ status: 200, contentType: 'video/x-matroska', body: Buffer.alloc(0) })
    );
    await page.route('**/api/archive/rms-detections/**', route =>
      route.fulfill({ json: { detections: [] } })
    );
    await page.route('**/stack/**', route =>
      route.fulfill({ status: 200, contentType: 'image/webp', body: FAKE_WEBP })
    );
    await page.route('**/fullstack/**', route =>
      route.fulfill({ status: 200, contentType: 'image/webp', body: FAKE_WEBP })
    );

    await login(page);
    await page.goto('/station/test_station_1');
    await waitForAuthApplied(page);
    await page.click('#tab-btn-archive');

    // Click the rendered archive card — its onclick calls archivePlayVideo with
    // the real filename, and archiveRender has populated _arcCardData (cam/date)
    // so the modal can derive the stack even though the payload carried none.
    const card = page.locator('#pane-archive .arc-card .arc-play').first();
    await expect(card).toBeVisible({ timeout: 5000 });
    await card.click();

    const backdrop = page.locator('.vm-backdrop.open').first();
    await expect(backdrop).toBeVisible({ timeout: 3000 });

    const stackDl = backdrop.locator('.vm-stack-dl-btn');
    await expect(stackDl).toBeVisible();
    // Download hands out the branded full-resolution stack, derived from the
    // clip identity since the payload carried no explicit stack URL.
    const href = await stackDl.getAttribute('href');
    expect(href).toContain('/fullstack/');
    expect(href).toContain('TST001_20260601_213000_stack.webp');

    expect(errors).toEqual([]);
  });
});
