/**
 * VDB must render and filter while its decoration fetches are still in flight.
 *
 * Regression cover for a panel that presented as a permanent "Loading…" with a
 * dead time filter. The clip grid used to await a 4-way Promise.all in which
 * /api/archive/indexed and /api/cached-files (ARCHIVED / CACHED badges) and the
 * twilight clamp were equal members, so the slowest of the four decided when
 * anything rendered. Until it resolved, vdbAllChunks stayed empty and the time
 * slider had nothing to filter. In production /api/archive/indexed took 11 s
 * behind a saturated HTTP/1.1 connection pool and gated the whole panel — with
 * no JS exception, which is why it never reproduced on an idle instance.
 *
 * The pre-existing vdb-*.spec.js files could not catch this: they assert only
 * that the slider handler exists and doesn't throw, and their grid assertions
 * sat behind `.vdb-tile`, a class the renderer never emits.
 *
 * Both scenarios here are gated on promises the test resolves, not on sleeps,
 * so there is no timing race.
 */
import { test, expect } from '@playwright/test';

const ADMIN_USER = 'admin_test';
const PASSWORD = 'test-e2e-pass';

const MOCK_NIGHTS = ['20260601'];
// 21:30 and 22:00 UTC, so a 21:45→23:00 window keeps exactly one.
const MOCK_CHUNKS = [
  { filename: 'TST001_20260601_213000_color.mkv', time: '21:30:00', size_mb: 12.5, meteor_time: null, stack: null },
  { filename: 'TST001_20260601_220000_color.mkv', time: '22:00:00', size_mb: 10.2, meteor_time: null, stack: null },
];
// Civil sunset 19:00 UTC, civil sunrise 03:00 UTC -> clamp 19:00..03:00, which
// spans both chunks. If this clamp lands after the user has narrowed the range
// it must NOT be applied.
const MOCK_TWILIGHT = {
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

function interceptChunks(page) {
  page.route('**/api/videodb/nights/**', route => route.fulfill({ json: MOCK_NIGHTS }));
  page.route('**/api/videodb/chunks/**', route => route.fulfill({ json: MOCK_CHUNKS }));
  page.route('**/api/cached-video/**', route =>
    route.fulfill({ status: 200, contentType: 'video/mp4', body: Buffer.alloc(0) }));
}

/** Open the VDB tab and wait for the clip grid. */
async function openVdb(page) {
  await page.goto('/station/test_station_1');
  await page.click('#tab-btn-videodb');
}

test.describe('VDB with slow decoration fetches', () => {
  test('clip grid renders and filters while the archive index is still pending', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    // Never released: the badge fetch stays pending for the whole test.
    let releaseArchive;
    const archiveGate = new Promise(res => { releaseArchive = res; });
    await page.route('**/api/archive/indexed/**', async route => {
      await archiveGate;
      await route.fulfill({ json: [] });
    });
    await page.route('**/api/twilight/**', route => route.fulfill({ json: MOCK_TWILIGHT }));
    interceptChunks(page);
    await login(page);
    await openVdb(page);

    // The grid must not wait on the badge fetch. Before the fix this timed out.
    const cards = page.locator('#vdb-results .vdb-chunk-card');
    await expect(cards).toHaveCount(2, { timeout: 5000 });
    // And the status must have left "Loading…" rather than sitting there.
    await expect(page.locator('#vdb-status')).toContainText(/\d+ clip/, { timeout: 5000 });

    // The time filter operates on the rendered chunks, so it works too.
    await page.fill('#vdb-time-start', '21:45');
    await page.dispatchEvent('#vdb-time-start', 'change');
    await expect(cards).toHaveCount(1, { timeout: 3000 });

    releaseArchive();
    expect(errors).toEqual([]);
  });

  test('a late twilight clamp does not discard the range the user set', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    // Held until the test has narrowed the range, then released — the exact
    // ordering that used to snap a typed window back to sunset..sunrise.
    let releaseTwilight;
    const twilightGate = new Promise(res => { releaseTwilight = res; });
    await page.route('**/api/twilight/**', async route => {
      await twilightGate;
      await route.fulfill({ json: MOCK_TWILIGHT });
    });
    await page.route('**/api/archive/indexed/**', route => route.fulfill({ json: [] }));
    interceptChunks(page);
    await login(page);
    await openVdb(page);

    const cards = page.locator('#vdb-results .vdb-chunk-card');
    await expect(cards).toHaveCount(2, { timeout: 5000 });

    await page.fill('#vdb-time-start', '21:45');
    await page.dispatchEvent('#vdb-time-start', 'change');
    await expect(cards).toHaveCount(1, { timeout: 3000 });

    // Now let the clamp land. It may draw its markers, but must leave the
    // user's range — and therefore the filtered grid — alone.
    releaseTwilight();
    await page.waitForTimeout(1000);
    await expect(page.locator('#vdb-time-start')).toHaveValue('21:45');
    await expect(cards).toHaveCount(1);

    expect(errors).toEqual([]);
  });

  test('twilight still clamps the range when the user has not touched it', async ({ page }) => {
    // The guard must be scoped to user edits only: on a plain night load the
    // clamp is still the correct owner of the range.
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await page.route('**/api/twilight/**', route => route.fulfill({ json: MOCK_TWILIGHT }));
    await page.route('**/api/archive/indexed/**', route => route.fulfill({ json: [] }));
    interceptChunks(page);
    await login(page);
    await openVdb(page);

    await expect(page.locator('#vdb-results .vdb-chunk-card')).toHaveCount(2, { timeout: 5000 });
    // Civil sunset 19:00 UTC / sunrise 03:00 UTC from MOCK_TWILIGHT, which has
    // no capture_* fields: the night falls back to civil sunrise + 30 min.
    await expect(page.locator('#vdb-time-start')).toHaveValue('19:00', { timeout: 5000 });
    await expect(page.locator('#vdb-time-end')).toHaveValue('03:30');

    expect(errors).toEqual([]);
  });
});
