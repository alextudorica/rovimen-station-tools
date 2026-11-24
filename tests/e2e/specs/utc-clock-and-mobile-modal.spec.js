import { test, expect } from '@playwright/test';

const ADMIN_USER = 'admin_test';
const PASSWORD = 'test-e2e-pass';

// "YYYY-MM-DD HH:MM:SS UTC" — the unambiguous label formatUtcClock() paints.
const CLOCK_RE = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC$/;

async function login(page, user = ADMIN_USER, pass = PASSWORD) {
  await page.goto('/login');
  await page.fill('#login-username', user);
  await page.fill('#login-password', pass);
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

// Every page shows the live UTC clock and tonight's RMS capture window in its
// header (the auth pages, which have no header, top-right), at desktop and
// phone sizes, without console errors.
const RMS_RE = /^RMS (starts \d{2}:\d{2} UTC|recording · until \d{2}:\d{2} UTC)$/;

test.describe('UTC clock and RMS window are on every page', () => {
  for (const [name, path, vp] of [
    ['fleet map', '/', null],
    ['detections page', '/events', null],
    ['highlights page', '/highlights', null],
    ['login page', '/login', null],
    ['fleet map at a phone viewport', '/', { width: 390, height: 844 }],
  ]) {
    test(`${name}: live UTC clock and RMS window visible, no console errors`, async ({ page }) => {
      const errors = [];
      page.on('pageerror', e => errors.push(e.message));
      if (vp) await page.setViewportSize(vp);
      await page.goto(path);
      const clock = page.locator('#utc-clock');
      await expect(clock).toBeVisible();
      await expect(clock).toHaveText(CLOCK_RE, { timeout: 3000 });
      const fontPx = await clock.evaluate(el => parseFloat(getComputedStyle(el).fontSize));
      expect(fontPx).toBeGreaterThanOrEqual(10);
      await expect(page.locator('.js-rms-window').first()).toHaveText(RMS_RE, { timeout: 3000 });
      await expect(page.locator('.js-rms-window').first()).toBeVisible();
      expect(errors).toEqual([]);
    });
  }
});

// ── Mobile clip modal: every action button must be reachable ────────────────
// Regression guard for the operator report "Pe PC apare / Pe telefon nu": on a
// phone the Download stack / Show stack buttons fell below the viewport fold
// because the modal grew taller than the screen with no visible scroll. The fix
// caps the dialog to the viewport and scrolls its body internally.

const MOCK_NIGHTS = ['20260601'];
const MOCK_CHUNKS = [
  {
    filename: 'TST001_20260601_213000_color.mkv',
    time: '21:30:00',
    size_mb: 12.5,
    meteor_time: '2026-06-01T21:30:05.123',
    detection_offset_s: 5.0,
    stack: 'TST001_20260601_213000_stack.webp',
    stack_subdir: 'stacks',
    locked: true,
    lock_type: 'detection',
    reencoded: false,
  },
];

function interceptStationAPIs(page) {
  page.route('**/api/videodb/nights/**', route => route.fulfill({ json: MOCK_NIGHTS }));
  page.route('**/api/videodb/chunks/**', route => route.fulfill({ json: MOCK_CHUNKS }));
  page.route('**/api/videodb/rms-detections/**', route => route.fulfill({ json: { detections: [] } }));
  page.route('**/api/cached-video/**', route => route.fulfill({ status: 200, contentType: 'video/mp4', body: Buffer.alloc(0) }));
  page.route('**/api/cached-files/**', route => route.fulfill({ json: [] }));
  page.route('**/api/status/**', route => route.fulfill({ json: { online: true } }));
  page.route('**/stack/**', route => route.fulfill({ status: 404, body: '' }));
  page.route('**/thumbnail/**', route => route.fulfill({ status: 404, body: '' }));
  page.route('**/api/platepar/**', route => route.fulfill({ json: {} }));
}

test.describe('clip modal action buttons on mobile', () => {
  test.beforeEach(async ({ page }) => {
    interceptStationAPIs(page);
    await login(page);
  });

  test('Download-stack and Show-stack buttons are visible and unclipped at 390x844', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/station/test_station_1');
    await page.click('#tab-btn-videodb');
    await page.waitForTimeout(1200);

    // Open a clip that HAS a stack, so both stack buttons are wired.
    await page.evaluate(() => window.vdbOpenModal(
      'test_station_1', 'TST001', '20260601',
      'TST001_20260601_213000_color.mkv', '21:30:00', 12.5,
    ));

    const modal = page.locator('.vm-backdrop.open');
    await expect(modal).toBeVisible({ timeout: 3000 });

    const showStack = modal.locator('.vm-stack-show-btn');
    const dlStack = modal.locator('.vm-stack-dl-btn');

    // On a phone the secondary actions live in the More bottom sheet.
    await modal.locator('.vm-more-btn').click();
    await page.waitForTimeout(300);   // let the sheet finish sliding up before measuring

    // Both stack buttons are rendered (not display:none) …
    await expect(showStack).toBeVisible();
    await expect(dlStack).toBeVisible();

    // … and reachable within the viewport (top-level fix: dialog fits the
    // 844px screen instead of overflowing to 913px and dropping them below
    // the fold). Assert each button's box sits inside the viewport.
    for (const [name, loc] of [['Show stack', showStack], ['Download stack', dlStack]]) {
      const box = await loc.boundingBox();
      expect(box, `${name} should have a bounding box`).toBeTruthy();
      expect(box.width, `${name} width`).toBeGreaterThan(0);
      expect(box.height, `${name} height`).toBeGreaterThan(0);
      expect(box.y, `${name} top within viewport`).toBeGreaterThanOrEqual(0);
      expect(box.y + box.height, `${name} bottom within 844px viewport`).toBeLessThanOrEqual(844);

      // Nothing overlays the button centre — it is tappable.
      const atCentre = await page.evaluate(({ x, y }) => {
        const el = document.elementFromPoint(x, y);
        return el ? { tag: el.tagName, cls: String(el.className) } : null;
      }, { x: box.x + box.width / 2, y: box.y + box.height / 2 });
      expect(atCentre, `${name} centre hit-tests to an element`).toBeTruthy();
    }

    // The dialog no longer exceeds the viewport height.
    const dialogH = await modal.locator('.vm-dialog').evaluate(el => el.getBoundingClientRect().height);
    expect(dialogH).toBeLessThanOrEqual(844);

    expect(errors).toEqual([]);
  });
});

// ── Mobile clip modal: the Close button stays on screen ─────────────────────
// The header row (Prev, title, Open station, Open in Video DB, Next, Close)
// did not shrink, so on a phone it ran past the dialog's right edge and the
// dialog's overflow:hidden cut Close off.
test.describe('clip modal Close button on mobile', () => {
  test.beforeEach(async ({ page }) => {
    interceptStationAPIs(page);
    await login(page);
  });

  for (const width of [360, 390]) {
    test(`Close is visible, tappable and closes the player at ${width}px`, async ({ page }) => {
      const errors = [];
      page.on('pageerror', e => errors.push(e.message));

      await page.setViewportSize({ width, height: 800 });
      await page.goto('/station/test_station_1');
      await page.click('#tab-btn-videodb');
      await page.waitForTimeout(1200);
      await page.evaluate(() => window.vdbOpenModal(
        'test_station_1', 'TST001', '20260601',
        'TST001_20260601_213000_color.mkv', '21:30:00', 12.5,
      ));

      const modal = page.locator('.vm-backdrop.open');
      await expect(modal).toBeVisible({ timeout: 3000 });
      await expect(modal.locator('.vm-open-links')).toBeVisible();
      const close = modal.locator('.vm-close-btn');
      await expect(close).toBeVisible();

      const box = await close.boundingBox();
      const dialog = await modal.locator('.vm-dialog').boundingBox();
      expect(box.x, 'Close left edge on screen').toBeGreaterThanOrEqual(0);
      expect(box.x + box.width, 'Close inside the dialog').toBeLessThanOrEqual(dialog.x + dialog.width);
      expect(box.x + box.width, 'Close inside the viewport').toBeLessThanOrEqual(width);
      const hit = await page.evaluate(({ x, y }) => {
        const el = document.elementFromPoint(x, y);
        return !!el && !!el.closest('.vm-close-btn');
      }, { x: box.x + box.width / 2, y: box.y + box.height / 2 });
      expect(hit, 'Close centre hit-tests to Close').toBe(true);

      await close.click();
      await expect(modal).toHaveCount(0);
      expect(errors).toEqual([]);
    });
  }
});
