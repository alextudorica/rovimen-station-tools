import { test, expect } from '@playwright/test';

// The Video DB controls card on a phone: the desktop card's own controls laid
// out as [camera][night + moon] / times + Whole night / the unchanged slider /
// sort + manual-locks chips. Before: a one-button "Single cam" toggle ("All
// cams" is desktop-only), the night select cut to "2026-10", the time row
// wrapping, and a 367 px card.

async function login(page) {
  await page.goto('/login');
  await page.fill('#login-username', 'admin_test');
  await page.fill('#login-password', 'test-e2e-pass');
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

test('Video DB card fits a phone and keeps its controls aligned', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route('**/api/videodb/nights/**', r => r.fulfill({ json: ['20261020', '20261019'] }));
  await page.route('**/api/videodb/chunks/**', r => r.fulfill({ json: [] }));
  await page.route('**/api/twilight/**', r => r.fulfill({ json: {
    civil_set_min: 960, nautical_set_min: 995, astro_set_min: 1030,
    astro_rise_min: 210, nautical_rise_min: 245, civil_rise_min: 280,
    illumination: 0.78, phase_name: 'Waxing Gibbous', phase_emoji: '\u{1F314}',
    moon_rise_min: 920, moon_set_min: 1265 } }));
  await login(page);
  await page.goto('/station/test_station_1/videodb', { waitUntil: 'load' });
  await expect(page.locator('#vdb-night')).toBeVisible();

  await expect(page.locator('#vdb-view-toggle')).toBeHidden();
  await expect(page.locator('#pane-videodb .vdb-filter-card > .card-title')).toBeHidden();
  const r = await page.evaluate(() => {
    const box = s => document.querySelector(s).getBoundingClientRect();
    const night = document.querySelector('#vdb-night');
    return {
      overflow: document.documentElement.scrollWidth - window.innerWidth,
      cardH: box('#pane-videodb .vdb-filter-card').height,
      row1: [box('#vdb-camera select').top, box('#vdb-night').top],
      nightClipped: night.scrollWidth > night.clientWidth + 1,
      row2: [box('#vdb-time-start').top, box('#vdb-time-end').top, box('#vdb-time-reset').top],
      chips: [box('#vdb-sort-btn').top, box('#pane-videodb .vdb-filter-right .vdb-toggle-label').top],
      slider: box('#pane-videodb .vdb-slider-wrap').width,
      card: box('#pane-videodb .vdb-filter-card').width,
    };
  });
  expect(r.overflow).toBeLessThanOrEqual(0);
  expect(Math.abs(r.row1[0] - r.row1[1]), 'camera and night pickers share a row').toBeLessThanOrEqual(1);
  expect(r.nightClipped, 'night picker shows the full date').toBe(false);
  expect(Math.max(...r.row2) - Math.min(...r.row2), 'times and Whole night share a row').toBeLessThanOrEqual(1);
  expect(Math.abs(r.chips[0] - r.chips[1]), 'sort and manual-locks chips share a row').toBeLessThanOrEqual(1);
  expect(r.row2[0], 'time row is below the pickers').toBeGreaterThan(r.row1[0] + 30);
  expect(r.slider, 'slider spans the card').toBeGreaterThan(r.card * 0.8);
  expect(r.cardH, 'card stays compact').toBeLessThan(320);

  // mockup wording on phones: "20 Oct · 78%", "↓ Newest first", "● N clips"
  await expect(page.locator('#vdb-night option:checked')).toHaveText(/^\d{1,2} [A-Z][a-z]{2} · 78%$/);
  await expect(page.locator('#vdb-sort-btn')).toHaveText(/↓\s*Newest first/);
  await expect(page.locator('#pane-videodb .vdb-sort-caret')).toBeHidden();
  await expect(page.locator('#pane-videodb .vdb-status-date')).toHaveCount(0);  // no clips → no count line yet

  // the moon is drawn inside the night picker, with its illumination
  const moon = page.locator('#pane-videodb .vdb-night-moon');
  await expect(moon).toBeVisible();
  await expect(moon.locator('svg.moon-svg')).toHaveCount(1);
  await expect(moon).toContainText('78%');
  await expect(moon).toHaveAttribute('title', 'Waxing Gibbous: 78% illuminated');
  expect(errors).toEqual([]);
});

// Desktop uses the same card: one control row (camera buttons, night picker,
// Single/All cams, times + Whole night), the slider, then the chips and count.
test('Video DB card on desktop: one aligned control row, same wording as phones', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.setViewportSize({ width: 1280, height: 860 });
  await page.route('**/api/videodb/nights/**', r => r.fulfill({ json: ['20261020'] }));
  await page.route('**/api/videodb/chunks/**', r => r.fulfill({ json: [] }));
  await login(page);
  await page.goto('/station/test_station_1/videodb', { waitUntil: 'load' });
  await expect(page.locator('#vdb-view-toggle')).toBeVisible();
  await expect(page.locator('#pane-videodb .vdb-filter-card > .card-title')).toBeHidden();
  await expect(page.locator('#vdb-sort-btn')).toHaveText(/↓\s*Newest first/);
  const r = await page.evaluate(() => {
    const cy = s => { const b = document.querySelector(s).getBoundingClientRect(); return b.top + b.height / 2; };
    return {
      overflow: document.documentElement.scrollWidth - window.innerWidth,
      row1: ['#vdb-camera', '#vdb-night', '#vdb-view-toggle', '#vdb-time-start', '#vdb-time-reset'].map(cy),
      foot: [cy('#vdb-sort-btn'), cy('#pane-videodb .vdb-filter-right .vdb-toggle-label')],
    };
  });
  expect(r.overflow).toBeLessThanOrEqual(0);
  expect(Math.max(...r.row1) - Math.min(...r.row1), 'controls share one row').toBeLessThanOrEqual(1);
  expect(Math.abs(r.foot[0] - r.foot[1])).toBeLessThanOrEqual(1);
  expect(errors).toEqual([]);
});
