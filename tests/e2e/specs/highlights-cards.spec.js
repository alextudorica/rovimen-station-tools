import { test, expect } from '@playwright/test';

// Highlights cards: one image per event (the brightest camera's stack, not a
// 2x2 mosaic), the magnitude as the headline coloured by brightness, the #1
// event as a large lead card, and each research report as a full-width
// feature. One click handler on the grid opens an event's sheet.

const PL = [['Ghirdoveni', 'RO000A'], ['Ciocârlia', 'RO000H'], ['Cluj', 'RO000P'], ['Vaslui', 'RO000M']];
const wit = (n, i, brightest = 1) => PL.slice(0, n).map(([label, cam], k) => ({
  camera: cam, station_label: label, host_key: 'test_station_1', date: '2026-10-20', filename: `${cam}_${i}.mkv`,
  stack_url: `/stack/m/${cam}_${i}.jpg`, clip_url: `/media/${cam}_${i}.mp4`, detection_offset_s: 5,
  apparent_magnitude: k === brightest ? -7 : -3,
}));
const EVENTS = [-6.5, -4.7, -2.3, -1.2, -0.5].map((mag, i) => ({
  id: `ev${i}`, time_utc: `2026-10-1${i}T20:10:05Z`, peak_magnitude: mag, magnitude_kind: 'apparent',
  shower: 'ORI', duration_s: 2.5, fireball: mag <= -4, confirmed: true, report: null, witnesses: wit(4, i),
}));
const DATA = { events: EVENTS, total_detections: 100, dual_station_events: 5, new_orbits: 3, orbits_month_to_date: 1,
  main_shower: 'ORI', active_cameras: 4, active_locations: 2, coverage_km2: 0, monthly_totals: [] };
const REPORT = { title: 'Fireball over Oltenia', summary: 'Seen by four cameras.', time_utc: '2026-10-09T20:49:45Z',
  status: 'final', url: '/fireball/20261009_204945', places: ['Ghirdoveni'], witnesses: wit(4, 9), cover_url: null };

// a 1x1 PNG: a card drops an image that fails to load
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=', 'base64');

async function mock(page) {
  let calls = 0;
  await page.route('**/api/highlights/reports', r => r.fulfill({ json: { reports: [REPORT] } }));
  // the page asks for the range a few nights at a time; one chunk carries the events
  await page.route('**/api/highlights/data**', r =>
    r.fulfill({ json: ++calls === 1 ? DATA : { ...DATA, events: [], total_detections: 0 } }));
  await page.route('**/stack/m/**', r => r.fulfill({ status: 200, contentType: 'image/png', body: PNG }));
}

test('event cards: one brightest-camera image, magnitude headline, #1 leads', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.setViewportSize({ width: 1280, height: 900 });
  await mock(page);
  await page.goto('/highlights');
  const cards = page.locator('#hl-grid .hl-ev:not(.skeleton)');
  await expect(cards).toHaveCount(5, { timeout: 8000 });
  // the grid re-renders as each chunk of the range lands: measure once all are in
  await expect(page.locator('#hl-submit')).toHaveText('Load', { timeout: 8000 });

  // one image per card, from the camera that saw it brightest (index 1, Ciocârlia)
  for (const c of await cards.all()) await expect(c.locator('.hl-mosaic img')).toHaveCount(1);
  await expect(cards.first().locator('.hl-mosaic img')).toHaveAttribute('src', '/stack/m/RO000H_0.jpg');

  // the lead card is twice as wide as the others
  const [lead, next] = [await cards.nth(0).boundingBox(), await cards.nth(1).boundingBox()];
  expect(lead.width).toBeGreaterThan(next.width * 1.8);

  // magnitude headline, coloured by tier; fireballs say so in words
  await expect(cards.nth(0).locator('.hl-ev-mag')).toHaveClass(/mag-fireball/);
  await expect(cards.nth(2).locator('.hl-ev-mag')).toHaveClass(/mag-bright/);
  await expect(cards.nth(4).locator('.hl-ev-mag')).not.toHaveClass(/mag-/);
  await expect(cards.nth(0).locator('.hl-ev-title')).toHaveText(/^Fireball · Orionids/);
  await expect(cards.nth(0).locator('.hl-ev-meta')).toContainText('Ghirdoveni + 3 more');

  // one delegated handler: click and Enter both open the event's sheet
  await cards.nth(1).click();
  await expect(page.locator('#hl-sheet')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.locator('#hl-sheet')).toBeHidden();
  await cards.nth(2).focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('#hl-sheet')).toBeVisible();
  expect(errors).toEqual([]);
});

test('research report is a full-width feature with the teal "Read the research"', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await mock(page);
  await page.goto('/highlights');
  const card = page.locator('#hl-research-grid .hl-rcard');
  await expect(card).toBeVisible({ timeout: 8000 });
  await expect(page.locator('#hl-submit')).toHaveText('Load', { timeout: 8000 });
  const [c, grid] = [await card.boundingBox(), await page.locator('#hl-research-grid').boundingBox()];
  expect(c.width).toBeGreaterThan(grid.width - 2);
  await expect(card.locator('.hl-btn.primary')).toHaveText('Read the research');
  await expect(card.locator('.hl-rcard-media img')).toHaveCount(1);
});

test('phone: the lead card is a normal one-column card', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mock(page);
  await page.goto('/highlights');
  const cards = page.locator('#hl-grid .hl-ev:not(.skeleton)');
  await expect(cards).toHaveCount(5, { timeout: 8000 });
  // the grid re-renders as each chunk of the range lands: measure once all are in
  await expect(page.locator('#hl-submit')).toHaveText('Load', { timeout: 8000 });
  const [a, b] = [await cards.nth(0).boundingBox(), await cards.nth(1).boundingBox()];
  expect(Math.abs(a.width - b.width)).toBeLessThanOrEqual(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
});

test('report card cycles its figures: 3 s each, a 0.5 s fade through black', async ({ page }) => {
  await page.clock.install();
  await page.setViewportSize({ width: 1280, height: 900 });
  await mock(page);
  const slides = ['a', 'b', 'c'].map(n => `/fireball/20261009_204945/${n}.webp`);
  await page.route('**/api/highlights/reports', r =>
    r.fulfill({ json: { reports: [{ ...REPORT, cover_url: slides[0], slides }] } }));
  await page.route('**/fireball/20261009_204945/*.webp', r => r.fulfill({ status: 200, contentType: 'image/png', body: PNG }));
  await page.goto('/highlights');
  const img = page.locator('#hl-research-grid .hl-rcard-media.is-slideshow img');
  await expect(img).toHaveAttribute('src', slides[0], { timeout: 8000 });
  await img.scrollIntoViewIfNeeded();

  await page.clock.runFor(3000);
  await expect(img).toHaveClass(/is-fading/);           // fading out
  await page.clock.runFor(250);
  await expect(img).toHaveAttribute('src', slides[1]);  // swapped while black
  await expect(img).not.toHaveClass(/is-fading/);       // fading back in
  expect(await img.evaluate(el => getComputedStyle(el).transitionDuration)).toBe('0.25s');
  await page.clock.runFor(3250);
  await expect(img).toHaveAttribute('src', slides[2]);
});

test('reduced motion: the report card keeps its first figure', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.clock.install();
  await mock(page);
  const slides = ['a', 'b'].map(n => `/fireball/20261009_204945/${n}.webp`);
  await page.route('**/api/highlights/reports', r =>
    r.fulfill({ json: { reports: [{ ...REPORT, cover_url: slides[0], slides }] } }));
  await page.route('**/fireball/20261009_204945/*.webp', r => r.fulfill({ status: 200, contentType: 'image/png', body: PNG }));
  await page.goto('/highlights');
  const img = page.locator('#hl-research-grid .hl-rcard-media img');
  await expect(img).toHaveAttribute('src', slides[0], { timeout: 8000 });
  await page.clock.runFor(7000);
  await expect(img).toHaveAttribute('src', slides[0]);
});
