/**
 * Players link back to where the clip lives, and the pages remember where the
 * user was.
 *
 * - Every single-clip player shows "Open station" / "Open in Video DB" once it
 *   knows the station, camera and night; the Video DB link carries a window
 *   around the clip and clip=, and ?clip= opens that clip's player on arrival.
 * - Multi-camera tiles say which station and camera they are and offer a
 *   per-clip download of the original.
 * - Detections returns to the same scroll position after a reload.
 *
 * Station APIs are mocked with page.route(); every test asserts zero pageerror.
 */
import { test, expect } from '@playwright/test';

const ADMIN_USER = 'admin_test';
const PASSWORD = 'test-e2e-pass';
const CLIP = 'TST001_20260601_220000_color.mkv';

async function login(page) {
  await page.goto('/login');
  await page.fill('#login-username', ADMIN_USER);
  await page.fill('#login-password', PASSWORD);
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

// The modal decides at open() whether the visitor is signed in; wait for the
// page's auth init (it unhides the admin nav entry for an admin session).
async function waitForAuthApplied(page) {
  await expect(page.locator('#nav-tools')).toBeVisible({ timeout: 10000 });
}

function chunksFor(cam, date) {
  return ['213000', '220000', '231500'].map(t => ({
    filename: `${cam}_${date}_${t}_color.mkv`,
    time: `${t.slice(0, 2)}:${t.slice(2, 4)}:${t.slice(4, 6)}`,
    size_mb: 10, meteor_time: null, stack: null,
  }));
}

async function mockVdb(page) {
  await page.route('**/api/videodb/nights/**', route => route.fulfill({ json: ['20260601'] }));
  await page.route('**/api/videodb/chunks/**', route => {
    const parts = new URL(route.request().url()).pathname.split('/');
    const date = parts.pop();
    const cam = parts.pop();
    return route.fulfill({ json: chunksFor(cam, date) });
  });
  await page.route('**/api/archive/indexed/**', route => route.fulfill({ json: [] }));
  await page.route('**/api/cached-files/**', route => route.fulfill({ json: [] }));
  await page.route('**/api/cached-video/**', route =>
    route.fulfill({ status: 200, contentType: 'video/mp4', body: Buffer.alloc(0) }));
}

test.describe('player links and deep links', () => {
  let errors;
  test.beforeEach(async ({ page }) => {
    errors = [];
    page.on('pageerror', e => errors.push(e.message));
  });
  test.afterEach(() => { expect(errors).toEqual([]); });

  test('the Detections player links to the station and to the Video DB at the clip', async ({ page }) => {
    await page.route('**/api/cached-video/**', route =>
      route.fulfill({ status: 200, contentType: 'video/mp4', body: Buffer.alloc(0) }));
    await login(page);
    await page.goto('/events');
    await waitForAuthApplied(page);
    await page.evaluate(clip => {
      window.openSingleVideo('test_station_1', 'TST001', '20260601', clip, 5.0, null);
    }, CLIP);
    const backdrop = page.locator('.vm-backdrop.open').first();
    await expect(backdrop).toBeVisible({ timeout: 3000 });
    await expect(backdrop.locator('.vm-download-btn')).toBeVisible();
    await expect(backdrop.locator('.vm-download-trim-btn')).toBeVisible();
    await expect(backdrop.locator('.vm-open-station'))
      .toHaveAttribute('href', `/station/test_station_1/rms?cam=TST001&date=20260601&clip=${CLIP}`);
    // 22:00:00 + 5 s, widened ±3 min to whole minutes.
    await expect(backdrop.locator('.vm-open-vdb'))
      .toHaveAttribute('href', `/station/test_station_1/videodb?cam=TST001&date=20260601&from=21:57&to=22:04&clip=${CLIP}`);
  });

  test('?clip= opens that clip in the Video DB on arrival', async ({ page }) => {
    await mockVdb(page);
    await login(page);
    await page.goto(`/station/test_station_1/videodb?cam=TST001&date=20260601&from=21:57&to=22:04&clip=${CLIP}`);
    const backdrop = page.locator('#vdb-modal-container .vm-backdrop.open');
    await expect(backdrop).toBeVisible({ timeout: 8000 });
    await expect(backdrop.locator('.vm-title')).toContainText('22:00:00');
    // The clip is consumed once: the canonical URL drops it.
    await expect(page).not.toHaveURL(/clip=/);
  });

  test('a multi-camera tile downloads the selection once the trim handles moved', async ({ page }) => {
    // Tile videos fail fast, so the tiles read "Unavailable" and the control
    // row (with the trim bar) comes up without playable media.
    await page.route('**/api/cached-video/**', route => route.fulfill({ status: 404, body: '' }));
    let trimmed = null;
    let original = null;
    await page.route('**/api/archive/shortclip/**', route => {
      trimmed = route.request().url();
      route.fulfill({ status: 200, contentType: 'video/mp4', body: Buffer.from([0]) });
    });
    await page.route('**/download/**', route => {
      original = route.request().url();
      route.fulfill({ status: 200, contentType: 'video/mp4', body: Buffer.from([0]) });
    });
    await login(page);
    await page.goto('/events');
    await waitForAuthApplied(page);
    await page.evaluate(() => {
      window.openEventModal({
        event_time: '2026-06-01T22:00:05', witness_count: 2, station_count: 2,
        witnesses: [
          { host_key: 'test_station_1', station_label: 'Test Station Alpha', cam: 'TST001',
            filename: 'TST001_20260601_220000_color.mkv', meteor_time: '2026-06-01T22:00:05',
            detection_offset_s: 5, stack: null },
          { host_key: 'test_station_2', station_label: 'Test Station Beta', cam: 'TST003',
            filename: 'TST003_20260601_215955_color.mkv', meteor_time: '2026-06-01T22:00:05',
            detection_offset_s: 10, stack: null },
        ],
      }, '20260601');
    });
    const ctrl = page.locator('.mdm-backdrop .mdm-ctrl-row');
    await expect(ctrl).toHaveCSS('visibility', 'visible', { timeout: 10000 });

    // Drag the selection's start from -2 s to about 0 s: the window runs
    // -2 .. +8 s (CLIP_PRE_S / CLIP_POST_S), so 0 s is 20 % along the bar.
    const bar = page.locator('.mdm-backdrop .mdm-trackbar');
    const box = await bar.boundingBox();
    const handle = page.locator('.mdm-backdrop .mdm-trackbar-trim-start');
    await handle.hover();
    const hbox = await handle.boundingBox();
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.2, hbox.y + hbox.height / 2, { steps: 8 });
    await page.mouse.up();

    await page.locator('.mdm-cap-dl').first().click();
    await expect.poll(() => trimmed, { timeout: 5000 }).not.toBeNull();
    expect(original).toBeNull();
    const u = new URL(trimmed);
    expect(u.pathname).toBe('/api/archive/shortclip/TST001/20260601/TST001_20260601_220000_color.mkv');
    // The clip's detection is 5 s in: a selection of ~[0, +8] s is ss ~5, t ~8.
    expect(parseFloat(u.searchParams.get('ss'))).toBeGreaterThan(4);
    expect(parseFloat(u.searchParams.get('ss'))).toBeLessThan(6);
    expect(parseFloat(u.searchParams.get('t'))).toBeGreaterThan(7);
    expect(parseFloat(u.searchParams.get('t'))).toBeLessThan(9);
  });

  test('multi-camera tiles are captioned and each downloads its original', async ({ page }) => {
    await page.route('**/api/cached-video/**', route =>
      route.fulfill({ status: 200, contentType: 'video/mp4', body: Buffer.alloc(0) }));
    let downloaded = null;
    await page.route('**/download/**', route => {
      downloaded = new URL(route.request().url()).pathname;
      route.fulfill({ status: 200, contentType: 'video/x-matroska', body: Buffer.from([0]) });
    });
    await login(page);
    await page.goto('/events');
    await waitForAuthApplied(page);
    await page.evaluate(() => {
      window.openEventModal({
        event_time: '2026-06-01T22:00:05', witness_count: 2, station_count: 2,
        witnesses: [
          { host_key: 'test_station_1', station_label: 'Test Station Alpha', cam: 'TST001',
            filename: 'TST001_20260601_220000_color.mkv', meteor_time: '2026-06-01T22:00:05',
            detection_offset_s: 5, stack: null },
          { host_key: 'test_station_2', station_label: 'Test Station Beta', cam: 'TST003',
            filename: 'TST003_20260601_215955_color.mkv', meteor_time: '2026-06-01T22:00:05',
            detection_offset_s: 10, stack: null },
        ],
      }, '20260601');
    });
    const caps = page.locator('.mdm-cap span');
    await expect(caps).toHaveText(['Test Station Alpha · TST001', 'Test Station Beta · TST003'], { timeout: 5000 });
    await page.locator('.mdm-cap-dl').first().click();
    await expect.poll(() => downloaded, { timeout: 5000 })
      .toBe('/download/test_station_1/TST001/20260601/TST001_20260601_220000_color.mkv');
  });
});

test.describe('Detections scroll position', () => {
  test('a reload returns to where the user had scrolled', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    const chunks = [];
    for (let i = 0; i < 400; i++) {
      const sec = 18 * 3600 + i * 20;      // 18:00:00 onwards, 20 s apart
      const hh = String(Math.floor(sec / 3600)).padStart(2, '0');
      const mm = String(Math.floor(sec / 60) % 60).padStart(2, '0');
      const ss = String(sec % 60).padStart(2, '0');
      chunks.push({
        filename: `TST001_20260601_${hh}${mm}${ss}_color.mkv`, time: `${hh}:${mm}:${ss}`,
        stack: null, meteor_time: `2026-06-01T${hh}:${mm}:${ss}`, detection_offset_s: 2,
        lock_type: 'detection', reencoded: true, pending: false,
      });
    }
    await page.route('**/api/detections/nights', route => route.fulfill({ json: ['20260601'] }));
    await page.route(/\/api\/detections\/\d{8}$/, route => route.fulfill({ json: {
      date: '20260601', correlation_window_s: 1, events: [], detections: [],
      by_station: { test_station_1: { label: 'Test Station Alpha', online: true, cameras: { TST001: chunks } } },
    } }));
    // Full-night window so every card is shown.
    await page.route('**/api/twilight/**', route => route.fulfill({ status: 404, body: '' }));
    await login(page);
    await page.goto('/events');
    await expect(page.locator('.ev-card-trigger').first()).toBeVisible({ timeout: 8000 });
    await page.evaluate(() => window.scrollTo(0, 400));
    await page.waitForFunction(() => window.scrollY >= 380);
    await page.reload();
    await expect(page.locator('.ev-card-trigger').first()).toBeAttached({ timeout: 8000 });
    await page.waitForFunction(() => window.scrollY >= 380, null, { timeout: 6000 });
    expect(errors).toEqual([]);
  });
});
