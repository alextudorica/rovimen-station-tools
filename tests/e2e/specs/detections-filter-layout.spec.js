import { test, expect } from '@playwright/test';

// The Detections filter bar uses the Video DB card's look: night picker with
// the drawn moon ("20 Oct · 78%"), 40 px time fields that open the shared
// time picker, "Whole night", the unchanged slider and the pointing compass.
// Phones: night + compass / times + Whole night / slider. Desktop: one row
// (the bar is sticky).

async function openEvents(page) {
  await page.route('**/api/detections/nights', r => r.fulfill({ json: ['20261020', '20261019'] }));
  await page.goto('/events', { waitUntil: 'load' });
  await expect(page.locator('#ev-date option')).toHaveCount(2);
  await expect(page.locator('#ev-time-start')).not.toHaveValue('12:00');
}

const centre = (page, sel) => page.locator(sel).evaluate(el => { const b = el.getBoundingClientRect(); return b.top + b.height / 2; });

test('Detections bar on desktop: one aligned row, short night label, typed start time', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.setViewportSize({ width: 1280, height: 860 });
  await openEvents(page);
  await expect(page.locator('#ev-date option:checked')).toHaveText(/^\d{1,2} [A-Z][a-z]{2}( · \d+%)?$/);
  await expect(page.locator('.ev-night-moon svg.moon-svg')).toHaveCount(1);
  const ys = await Promise.all(['#ev-date', '#ev-time-start', '#ev-time-reset', '.ev-slider-wrap', '.ev-compass'].map(s => centre(page, s)));
  expect(Math.max(...ys) - Math.min(...ys), 'controls share one row').toBeLessThanOrEqual(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);

  // a click selects the field; the time is typed straight in
  await page.click('#ev-time-start');
  await expect(page.locator('.tp-is-pop .tp-panel')).toBeVisible();
  await expect(page.locator('#ev-time-start')).toBeFocused();
  await page.keyboard.type('2000');
  await page.keyboard.press('Enter');
  await expect(page.locator('#ev-time-start')).toHaveValue('20:00');
  await expect(page).toHaveURL(/from=20(%3A|:)00/);

  // Whole night gives the window back to the twilight clamp
  await page.click('#ev-time-reset');
  await expect(page.locator('#ev-time-start')).not.toHaveValue('20:00');
  await expect(page).not.toHaveURL(/from=/);
  expect(errors).toEqual([]);
});

test.describe('Detections bar on a touch phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });

  test('night + compass, then times + Whole night; tapping a time opens the sheet without selecting text', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await openEvents(page);
    const night = await centre(page, '#ev-date');
    const compass = await centre(page, '.ev-compass');
    const time = await centre(page, '#ev-time-start');
    const reset = await centre(page, '#ev-time-reset');
    expect(Math.abs(night - compass), 'night picker and compass share row 1').toBeLessThanOrEqual(1);
    expect(Math.abs(time - reset), 'times and Whole night share row 2').toBeLessThanOrEqual(1);
    expect(time).toBeGreaterThan(night + 30);
    await expect(page.locator('.ev-filter-bar .ev-zoom-row')).toBeHidden();
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);

    await page.tap('#ev-time-start');
    await expect(page.locator('.tp-is-sheet')).toHaveCount(1);
    const sel = await page.locator('#ev-time-start').evaluate(el => el.selectionEnd - el.selectionStart);
    expect(sel, 'no text selected').toBe(0);
    expect(errors).toEqual([]);
  });
});
