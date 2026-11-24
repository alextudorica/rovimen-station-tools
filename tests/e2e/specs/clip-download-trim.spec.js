import { test, expect } from '@playwright/test';

// Regression guard: once the operator has dragged the trim handles, the
// VideoModal's main download button downloads that selection (events →
// /api/archive/shortclip, VDB → /shortclip, both with ?ss=&t=) and the second
// button the whole clip (/download/…). Untouched, "Download clip" is the whole
// clip and "Download trimmed" the preset window.
//
// These tests open a modal, drag the start handle to introduce a sub-selection,
// click the main button, and assert the request the browser fires carries ss/t
// matching the selection. The download response is intercepted so no real clip
// is needed. Every test asserts zero pageerror.

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
];

// A tiny non-empty body so blob() resolves; the browser never plays it.
const FAKE_MP4 = Buffer.from([0, 0, 0, 0]);

async function login(page) {
  await page.goto('/login');
  await page.fill('#login-username', ADMIN_USER);
  await page.fill('#login-password', PASSWORD);
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

// VideoModal decides ONCE, at open(), whether to show its operator-only
// actions (Download clip, Crop, Download stack): `_applyAnonControlGate()`
// hides them when `state.AUTH_USER` is still null, and nothing re-runs the gate
// when /api/auth/status finally lands. `domcontentloaded` fires long before
// that fetch resolves, so driving the modal straight after a goto races it and
// silently yields a public-visitor modal with the download button hidden.
// Wait for the fetch's DOM effect instead — the admin nav entry, which every
// page's auth init unhides for an admin session.
async function waitForAuthApplied(page) {
  await expect(page.locator('#nav-tools')).toBeVisible({ timeout: 10000 });
}

// The modal's `loadedmetadata` handler rewrites _trim.duration/_trim.end and
// calls _trimDraw(), which repositions the trim handles. It fires whenever the
// stubbed <video> settles, which is asynchronous and unordered relative to the
// backdrop becoming visible — so a drag started too early reads the handle's
// pre-draw bounding box, the pointerdown lands where the handle no longer is,
// and the drag silently no-ops (ss stays 0). Wait for the handler's observable
// effect instead: the video element reports metadata, or reports an error (the
// stubbed bodies are not decodable, so `error` is the usual settle path).
async function waitForVideoSettled(page) {
  await page.waitForFunction(() => {
    const v = document.querySelector('.vm-backdrop.open .vm-video-el');
    if (!v) return false;
    return !v.src || v.readyState >= 1 || v.error !== null;
  }, null, { timeout: 10000 });
}

// Drag the start handle right to a fraction of the trackbar so start > 0. The
// modal reads _trim.start (a fraction of duration) from the pointer x, so the
// resulting ss is duration * fraction.
async function dragStartHandle(page, fraction) {
  const bar = page.locator('.vm-backdrop.open .vm-trackbar').first();
  await expect(bar).toBeVisible({ timeout: 3000 });
  await waitForVideoSettled(page);

  const handle = page.locator('.vm-backdrop.open .vm-trackbar-start-handle').first();
  // hover() runs Playwright's actionability checks — visible, and a bounding
  // box stable across two animation frames — and parks the pointer on the
  // handle. This is the piece raw page.mouse coordinates skipped: it is what
  // guarantees the pointerdown below actually lands on the handle.
  await handle.hover();

  const box = await bar.boundingBox();
  expect(box, 'trackbar must have a bounding box').toBeTruthy();
  const hbox = await handle.boundingBox();
  const startY = hbox.y + hbox.height / 2;
  const targetX = box.x + box.width * fraction;
  await page.mouse.down();
  await page.mouse.move(targetX, startY, { steps: 8 });
  await page.mouse.up();

  // The drag's own observable effect: the start label leaves 0.0s. Asserting it
  // here fails at the drag rather than 20 lines later at an opaque `ss === 0`.
  await expect(page.locator('.vm-backdrop.open .vm-lbl-start').first())
    .not.toHaveText('0.0s', { timeout: 3000 });
}

// Parse ss/t out of an intercepted shortclip request URL.
function parseSsT(url) {
  const u = new URL(url);
  return {
    ss: parseFloat(u.searchParams.get('ss')),
    t: parseFloat(u.searchParams.get('t')),
  };
}

test.describe('clip download honours the trim selection', () => {
  test('events modal → /api/archive/shortclip carries ss/t matching the sub-selection', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    // Intercept the trimmed archive download and capture its URL.
    let dlUrl = null;
    await page.route('**/api/archive/shortclip/**', route => {
      dlUrl = route.request().url();
      route.fulfill({
        status: 200,
        contentType: 'video/mp4',
        headers: {
          'Content-Disposition': 'attachment; filename="TST001_20260601_213000_clip.mp4"',
        },
        body: FAKE_MP4,
      });
    });
    // The modal loads /api/cached-video for playback — stub it so the page is quiet.
    await page.route('**/api/cached-video/**', route =>
      route.fulfill({ status: 200, contentType: 'video/mp4', body: Buffer.alloc(0) })
    );

    await login(page);
    await page.goto('/events');
    await page.waitForLoadState('domcontentloaded');
    await waitForAuthApplied(page);

    // Open the single-clip modal directly via the page's own entry point.
    await page.evaluate(() => {
      window.openSingleVideo(
        'test_station_1', 'TST001', '20260601',
        'TST001_20260601_213000_color.mkv', 2.0, null
      );
    });
    const backdrop = page.locator('.vm-backdrop.open').first();
    await expect(backdrop).toBeVisible({ timeout: 3000 });

    // Drag the start handle to ~35% → ss ≈ duration*0.35, end stays at duration.
    // _trim.duration defaults to 20 s until the (stubbed) video reports metadata.
    await dragStartHandle(page, 0.35);

    // The main button now names and downloads the selection.
    const dlBtn = page.locator('.vm-backdrop.open .vm-download-btn').first();
    await expect(dlBtn).toContainText('Download selection');
    await dlBtn.click();

    await expect.poll(() => dlUrl, { timeout: 5000 }).not.toBeNull();
    expect(dlUrl).toContain('/api/archive/shortclip/TST001/20260601/');
    const { ss, t } = parseSsT(dlUrl);
    // A real sub-selection: start moved off zero, window shorter than full.
    expect(ss).toBeGreaterThan(0);
    // The default full duration is 20 s; a 35% start leaves ~13 s.
    expect(ss).toBeLessThan(20);
    expect(t).toBeGreaterThanOrEqual(0.5);
    expect(t).toBeLessThan(20);
    // ss + t must not overrun the clip.
    expect(ss + t).toBeLessThanOrEqual(20.5);

    expect(errors).toEqual([]);
  });

  test('VDB modal → /shortclip carries ss/t matching the sub-selection', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await page.route('**/api/videodb/nights/**', route => route.fulfill({ json: MOCK_NIGHTS }));
    await page.route('**/api/videodb/chunks/**', route => route.fulfill({ json: MOCK_CHUNKS }));
    await page.route('**/api/cached-video/**', route =>
      route.fulfill({ status: 200, contentType: 'video/mp4', body: Buffer.alloc(0) })
    );

    let dlUrl = null;
    await page.route('**/shortclip/**', route => {
      dlUrl = route.request().url();
      route.fulfill({
        status: 200,
        contentType: 'video/mp4',
        headers: {
          'Content-Disposition': 'attachment; filename="TST001_20260601_213000_clip.mp4"',
        },
        body: FAKE_MP4,
      });
    });

    await login(page);
    await page.goto('/station/test_station_1');
    await waitForAuthApplied(page);
    await page.click('#tab-btn-videodb');

    const clip = page.locator('.vdb-chunk-card').first();
    await expect(clip).toBeVisible({ timeout: 5000 });
    await clip.locator('.vdb-chunk-nostack, img').first().click();

    const backdrop = page.locator('.vm-backdrop.open').first();
    await expect(backdrop).toBeVisible({ timeout: 3000 });

    await dragStartHandle(page, 0.40);

    const dlBtn = page.locator('.vm-backdrop.open .vm-download-btn').first();
    await expect(dlBtn).toContainText('Download selection');
    await dlBtn.click();

    await expect.poll(() => dlUrl, { timeout: 5000 }).not.toBeNull();
    // Station-first route (not the archive one).
    expect(dlUrl).toContain('/shortclip/test_station_1/TST001/20260601/');
    const { ss, t } = parseSsT(dlUrl);
    expect(ss).toBeGreaterThan(0);
    expect(ss).toBeLessThan(20);
    expect(t).toBeGreaterThanOrEqual(0.5);
    expect(t).toBeLessThan(20);
    expect(ss + t).toBeLessThanOrEqual(20.5);

    expect(errors).toEqual([]);
  });

  test('events modal: after a drag the second button downloads the whole clip', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    let originalUrl = null;
    await page.route('**/download/**', route => {
      originalUrl = route.request().url();
      route.fulfill({
        status: 200, contentType: 'video/mp4',
        headers: { 'Content-Disposition': 'attachment; filename="TST001_20260601_213000_color.mp4"' },
        body: FAKE_MP4,
      });
    });
    let dlUrl = null;
    await page.route('**/api/archive/shortclip/**', route => {
      dlUrl = route.request().url();
      route.fulfill({ status: 200, contentType: 'video/mp4', body: FAKE_MP4 });
    });
    await page.route('**/api/cached-video/**', route =>
      route.fulfill({ status: 200, contentType: 'video/mp4', body: Buffer.alloc(0) })
    );

    await login(page);
    await page.goto('/events');
    await waitForAuthApplied(page);
    await page.evaluate(() => {
      window.openSingleVideo(
        'test_station_1', 'TST001', '20260601',
        'TST001_20260601_213000_color.mkv', 2.0, null
      );
    });
    await expect(page.locator('.vm-backdrop.open').first()).toBeVisible({ timeout: 3000 });
    await dragStartHandle(page, 0.35);

    const whole = page.locator('.vm-backdrop.open .vm-download-trim-btn').first();
    await expect(whole).toContainText('Download whole clip');
    await whole.click();
    await expect.poll(() => originalUrl, { timeout: 5000 }).not.toBeNull();
    expect(new URL(originalUrl).pathname)
      .toBe('/download/test_station_1/TST001/20260601/TST001_20260601_213000_color.mkv');
    expect(dlUrl).toBeNull();
    expect(errors).toEqual([]);
  });

  test('events modal: Download clip is the original file, Download trimmed the full window', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    let originalUrl = null;
    await page.route('**/download/**', route => {
      originalUrl = route.request().url();
      route.fulfill({
        status: 200,
        contentType: 'video/x-matroska',
        headers: { 'Content-Disposition': 'attachment; filename="TST001_20260601_213000_color.mkv"' },
        body: FAKE_MP4,
      });
    });
    let dlUrl = null;
    await page.route('**/api/archive/shortclip/**', route => {
      dlUrl = route.request().url();
      route.fulfill({
        status: 200,
        contentType: 'video/mp4',
        headers: { 'Content-Disposition': 'attachment; filename="clip.mp4"' },
        body: FAKE_MP4,
      });
    });
    await page.route('**/api/cached-video/**', route =>
      route.fulfill({ status: 200, contentType: 'video/mp4', body: Buffer.alloc(0) })
    );

    await login(page);
    await page.goto('/events');
    await page.waitForLoadState('domcontentloaded');
    await waitForAuthApplied(page);

    await page.evaluate(() => {
      window.openSingleVideo(
        'test_station_1', 'TST001', '20260601',
        'TST001_20260601_213000_color.mkv', 2.0, null
      );
    });
    await expect(page.locator('.vm-backdrop.open').first()).toBeVisible({ timeout: 3000 });

    // Download clip: the untouched original, no re-encode, no trim params.
    await page.locator('.vm-backdrop.open .vm-download-btn').first().click();
    await expect.poll(() => originalUrl, { timeout: 5000 }).not.toBeNull();
    expect(new URL(originalUrl).pathname)
      .toBe('/download/test_station_1/TST001/20260601/TST001_20260601_213000_color.mkv');
    expect(dlUrl).toBeNull();

    // No handle drag: Download trimmed with the default full window.
    const dlBtn = page.locator('.vm-backdrop.open .vm-download-trim-btn').first();
    await expect(dlBtn).toBeVisible();
    await dlBtn.click();

    await expect.poll(() => dlUrl, { timeout: 5000 }).not.toBeNull();
    const { ss, t } = parseSsT(dlUrl);
    // Full window: starts at 0, spans the whole (default 20 s) clip.
    expect(ss).toBe(0);
    expect(t).toBeGreaterThan(0);

    expect(errors).toEqual([]);
  });
});
