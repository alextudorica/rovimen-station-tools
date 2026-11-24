import { test, expect } from '@playwright/test';

// Detections → Grid and Sort & Filter in the shared control look: 40 px
// controls on one row on desktop, labels inside the pickers, teal toggles,
// nothing scrolling sideways on a phone. The Grid's From / To open the shared
// time picker; the card-size slider (detection cards only) hides on the Grid.

const DATE = '20260601';
const FOOTAGE = {
  date: DATE, bucket_min: 1, max_window_min: 5, degraded: false,
  columns: [{ cam: 'RO000A', host_key: 'test_station_1', station_label: 'Test Station Alpha' }],
  rows: [{
    bucket_start_min: 22 * 60, label: '22:00', total: 1, station_count: 1, has_detection: false,
    cells: { RO000A: { host_key: 'test_station_1', count: 1, has_detection: false, chunks: [{
      host_key: 'test_station_1', filename: 'RO000A_20260601_220000_color.mkv', time: '22:00:00',
      stack: null, locked: false, meteor_time: null, detection_offset_s: 0 }] } },
  }],
};
const RANGE = ['ORI', 'ORI', 'STA', 'SPO'].map((shower, i) => ({
  host_key: 'test_station_1', cam: 'RO000A', date: '20261020',
  filename: `RO000A_20261020_22${i}000_color.mkv`, stack: null,
  meteor_time: `2026-10-20T22:${i}0:05`, station_label: 'Test Station Alpha', detection_offset_s: 5,
  rms: { shower, mag_apparent: -1 + i, duration_s: 0.5, angular_velocity: 20 },
}));

async function login(page) {
  await page.goto('/login');
  await page.fill('#login-username', 'admin_test');
  await page.fill('#login-password', 'test-e2e-pass');
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

async function routes(page, footageUrls = []) {
  // most recently registered route wins: catch-all first
  await page.route('**/api/detections/**', r => r.fulfill({ json: { date: DATE, detections: [], events: [] } }));
  await page.route('**/api/detections/nights', r => r.fulfill({ json: [DATE] }));
  await page.route('**/api/detections/range**', r => r.fulfill({ json: { detections: RANGE } }));
  await page.route('**/api/footage/grid/**', r => { footageUrls.push(r.request().url()); return r.fulfill({ json: FOOTAGE }); });
}

/** Tops and heights of the given controls. */
const boxes = (page, ids) => page.evaluate(ids => ids.map(id => {
  const r = document.getElementById(id).getBoundingClientRect();
  return { id, top: r.top, h: r.height };
}), ids);

const overflow = page => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);

test.describe('Grid tab', () => {
  test('desktop: one row of 40 px controls; a time typed into From reloads', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    const urls = [];
    await page.setViewportSize({ width: 1280, height: 900 });
    await routes(page, urls);
    await login(page);
    await page.goto('/events');
    await page.click('#tab-btn-grid');
    await expect(page.locator('#grid-from')).not.toHaveValue('', { timeout: 5000 });

    const b = await boxes(page, ['grid-mode', 'grid-bucket', 'grid-from', 'grid-to', 'grid-toggle-other', 'grid-select-toggle']);
    for (const x of b) expect(x.h, `${x.id} height`).toBeGreaterThanOrEqual(39);
    expect(Math.max(...b.map(x => x.top)) - Math.min(...b.map(x => x.top)), 'toolbar on one row').toBeLessThanOrEqual(1);
    await expect(page.locator('.ev-filter-bar .ev-zoom-row')).toBeHidden();
    await expect(page.locator('#grid-size')).toBeVisible();

    // a click brings the wheels up with the caret in From; a typed "2200" + Enter reloads
    await page.click('#grid-from');
    await expect(page.locator('.tp-is-pop')).toHaveCount(1);
    await expect(page.locator('#grid-from')).toBeFocused();
    const before = urls.length;
    await page.keyboard.type('2200');
    await page.keyboard.press('Enter');
    await expect(page.locator('#grid-from')).toHaveValue('22:00');
    await expect.poll(() => urls.length).toBeGreaterThan(before);
    expect(urls.at(-1)).toContain('from=22%3A00');

    // the card-size slider is back on the other tabs
    await page.click('#tab-btn-detections');
    await expect(page.locator('.ev-filter-bar .ev-zoom-row')).toBeVisible();
    expect(errors).toEqual([]);
  });

  test('phone: compact toolbar, read-only times, nothing scrolls sideways', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await routes(page);
    await login(page);
    await page.goto('/events');
    await page.click('#tab-btn-grid');
    await expect(page.locator('#grid-from')).not.toHaveValue('', { timeout: 5000 });

    expect(await overflow(page)).toBeLessThanOrEqual(0);
    const [mode, bucket, from, to] = await boxes(page, ['grid-mode', 'grid-bucket', 'grid-from', 'grid-to']);
    expect(Math.abs(mode.top - bucket.top), 'View and Bucket share a row').toBeLessThanOrEqual(1);
    expect(Math.abs(from.top - to.top), 'From and To share a row').toBeLessThanOrEqual(1);
    expect(from.top, 'times below View / Bucket').toBeGreaterThan(mode.top + 30);
    expect(await page.locator('#grid-from').evaluate(el => el.readOnly)).toBe(true);
    await expect(page.locator('#grid-size')).toBeHidden();
    await expect(page.locator('#pane-grid .grid-hint')).toBeHidden();
  });
});

test.describe('Sort & Filter tab', () => {
  test('desktop: dates and Sort on one row, shower chips toggle', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.setViewportSize({ width: 1280, height: 900 });
    await routes(page);
    await login(page);
    await page.goto('/events');
    await page.click('#tab-btn-sortfilter');
    const chips = page.locator('.sf-chip');
    await expect(chips).toHaveCount(3);   // ORI, STA, SPO

    const b = await boxes(page, ['sf-from', 'sf-to', 'sf-sort']);
    for (const x of b) expect(x.h, `${x.id} height`).toBeGreaterThanOrEqual(39);
    expect(Math.max(...b.map(x => x.top)) - Math.min(...b.map(x => x.top)), 'toolbar on one row').toBeLessThanOrEqual(1);

    const ori = page.locator('.sf-chip[data-shower="ORI"]');
    await ori.click();
    await expect(page.locator('.sf-chip[data-shower="ORI"]')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('.sf-section-sub').first()).toHaveText(/^2 of 4 shown$/);
    await page.locator('.sf-chip[data-shower="ORI"]').click();
    await expect(page.locator('.sf-chip[data-shower="ORI"]')).toHaveAttribute('aria-pressed', 'false');
    expect(errors).toEqual([]);
  });

  test('phone: dates share a row, no card-size slider, nothing scrolls sideways', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await routes(page);
    await login(page);
    await page.goto('/events');
    await page.click('#tab-btn-sortfilter');
    await expect(page.locator('.sf-chip')).toHaveCount(3);

    expect(await overflow(page)).toBeLessThanOrEqual(0);
    const [from, to, sort] = await boxes(page, ['sf-from', 'sf-to', 'sf-sort']);
    expect(Math.abs(from.top - to.top), 'From and To share a row').toBeLessThanOrEqual(1);
    expect(sort.top, 'Sort below the dates').toBeGreaterThan(from.top + 30);
    await expect(page.locator('.sf-toolbar .ev-zoom-row')).toBeHidden();
  });
});

// /events?shower=CODE comes from the Showers page. The shower stays in the URL,
// an out-of-season shower opens on the week around its last peak, and a
// selected shower with nothing in range stays selected (count 0) instead of
// silently turning the filter off.
test.describe('Sort & Filter from a shower link', () => {
  const MDC = {
    ORI: { name: 'Orionids', peak: 'Oct 21', activity_begin: 'Oct 2', activity_end: 'Nov 7', zhr: 20 },
    PER: { name: 'Perseids', peak: 'Aug 9', activity_begin: 'Jul 15', activity_end: 'Sep 14', zhr: 100 },
    QUA: { name: 'Quadrantids', peak: 'Jan 3', activity_begin: 'Dec 28', activity_end: 'Jan 12', zhr: 110 },
  };
  async function open(page, code, ranges) {
    await page.clock.install({ time: new Date('2026-10-21T12:00:00Z') });
    await routes(page);
    await page.route('**/api/showers', r => r.fulfill({ json: MDC }));
    await page.route('**/api/detections/range**', r => {
      ranges.push(new URL(r.request().url()).search);
      return r.fulfill({ json: { detections: RANGE } });
    });
    await login(page);
    await page.goto(`/events?shower=${code}`);
    await expect(page.locator('#tab-btn-sortfilter')).toHaveAttribute('aria-selected', 'true');
  }

  test('an active shower: the last 7 days, filtered, and the URL keeps it', async ({ page }) => {
    const ranges = [];
    await open(page, 'ori', ranges);
    await expect(page.locator('.sf-chip[data-shower="ORI"]')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('.sf-section-sub').first()).toHaveText(/^2 of 4 shown$/);
    await expect(page.locator('#sf-from')).toHaveValue('2026-10-15');
    await expect(page).toHaveURL(/[?&]shower=ORI(&|$)/);
    await expect(page).toHaveURL(/[?&]tab=sortfilter/);
  });

  test('an out-of-season shower opens on the week around its last peak', async ({ page }) => {
    const ranges = [];
    await open(page, 'PER', ranges);
    await expect(page.locator('#sf-from')).toHaveValue('2026-08-06');
    await expect(page.locator('#sf-to')).toHaveValue('2026-08-12');
    await expect.poll(() => ranges.at(-1)).toContain('from=20260806&to=20260812');
    // PER isn't in the mocked data: still selected, count 0, and said so
    const chip = page.locator('.sf-chip[data-shower="PER"]');
    await expect(chip).toHaveAttribute('aria-pressed', 'true');
    await expect(chip.locator('.sf-chip-count')).toHaveText('0');
    await expect(page.locator('#sortfilter-main .ev-empty').first()).toContainText('No PER detections');
    await expect(page).toHaveURL(/[?&]shower=PER(&|$)/);
    // clicking the chip clears the filter and the URL
    await chip.click();
    await expect(page.locator('.sf-section-sub').first()).toHaveText(/^4 of 4$/);
    await expect(page).not.toHaveURL(/shower=/);
  });

  test('a shower across New Year whose last peak was in January', async ({ page }) => {
    const ranges = [];
    await open(page, 'QUA', ranges);
    await expect(page.locator('#sf-from')).toHaveValue('2025-12-31');
    await expect(page.locator('#sf-to')).toHaveValue('2026-01-06');
  });
});
