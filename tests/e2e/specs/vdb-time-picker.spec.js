import { test, expect } from '@playwright/test';

// The Video DB time fields: on desktop a click selects the field and a time is
// typed straight in ("2333" = 23:33, Enter/Tab applies); ↓ opens the
// hour/minute wheels as a ~300 px pop-up. On phones the fields are read-only
// and a tap opens the wheels as a bottom sheet. Set applies both ends through
// the normal input path.

async function login(page) {
  await page.goto('/login');
  await page.fill('#login-username', 'admin_test');
  await page.fill('#login-password', 'test-e2e-pass');
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

// Desktop: the wheels open from the field with ↓; phones: a tap opens them.
async function openWheels(page, sel, sheet = false) {
  await page.click(sel);
  if (!sheet) await page.keyboard.press('ArrowDown');
}

/** Turn a wheel to a value. The hour wheel lists only the hours of the
 *  night's capture window, so a value's row depends on the night. */
const pickWheel = (page, kind, v) => page.locator(`.tp-wheel-${kind}`).evaluate((w, val) => {
  const i = [...w.children].findIndex(c => c.dataset.v === String(val));
  w.scrollTop = 44 * i;
}, v);

async function openVdb(page) {
  await page.route('**/api/videodb/nights/**', r => r.fulfill({ json: ['20261020'] }));
  await page.route('**/api/videodb/chunks/**', r => r.fulfill({ json: [] }));
  await login(page);
  await page.goto('/station/test_station_1/videodb', { waitUntil: 'load' });
  await expect(page.locator('#vdb-time-start')).toBeVisible();
  // the night's twilight clamp moves the window off its 12:00 default
  await expect(page.locator('#vdb-time-start')).not.toHaveValue('12:00');
}

for (const [name, vp, sheet] of [['phone', { width: 390, height: 844 }, true], ['desktop', { width: 1280, height: 860 }, false]]) {
  test(`time picker (${name}): pick start and end, Set applies both`, async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.setViewportSize(vp);
    await openVdb(page);
    expect(await page.locator('#vdb-time-start').evaluate(el => el.readOnly)).toBe(sheet);

    await openWheels(page, '#vdb-time-start', sheet);
    const panel = page.locator('.tp-panel');
    await expect(panel).toBeVisible();
    await expect(page.locator(sheet ? '.tp-is-sheet' : '.tp-is-pop')).toHaveCount(1);
    const box = await panel.boundingBox();
    if (sheet) expect(box.width).toBeGreaterThan(vp.width - 2);
    else expect(box.width).toBeLessThanOrEqual(320);
    // only the night's hours (the station's footage window), in night order
    const hours = await page.locator('.tp-wheel-h .tp-item').allTextContents();
    expect(hours, 'no daytime hours').not.toContain('12');
    expect(hours.indexOf('00')).toBeGreaterThan(hours.indexOf('23'));
    expect(+hours[0], 'starts in the evening').toBeGreaterThanOrEqual(15);

    await pickWheel(page, 'h', 19);
    await pickWheel(page, 'm', 0);
    await expect(page.locator('.tp-side[data-side="start"] b')).toHaveText('19:00');
    await page.click('.tp-side[data-side="end"]');
    await pickWheel(page, 'h', 2);
    await pickWheel(page, 'm', 30);
    await expect(page.locator('.tp-side[data-side="end"] b')).toHaveText('02:30');
    await page.click('.tp-set');

    await expect(page.locator('.tp-root')).toHaveCount(0);
    await expect(page.locator('#vdb-time-start')).toHaveValue('19:00');
    await expect(page.locator('#vdb-time-end')).toHaveValue('02:30');
    expect(errors).toEqual([]);
  });
}

test('time picker: every minute on the wheel, wheels drag with the mouse', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 860 });
  await openVdb(page);
  const field = await page.locator('#vdb-time-start').inputValue();
  await openWheels(page, '#vdb-time-start');
  await expect(page.locator('.tp-wheel-m .tp-item')).toHaveCount(60);
  await expect(page.locator('.tp-wheel-m .tp-item').nth(45)).toHaveText('45');
  // the field's minute is kept as it is (16:37 stays 16:37)
  await expect(page.locator('.tp-side[data-side="start"] b')).toHaveText(field);
  const before = await page.locator('.tp-side[data-side="start"] b').textContent();
  const w = await page.locator('.tp-wheel-h').boundingBox();
  await page.mouse.move(w.x + w.width / 2, w.y + w.height / 2);
  await page.mouse.down();
  await page.mouse.move(w.x + w.width / 2, w.y + w.height / 2 - 44 * 2 - 10, { steps: 8 });  // drag up two rows
  await page.mouse.up();
  const hBefore = +before.slice(0, 2);
  await expect(page.locator('.tp-side[data-side="start"] b')).toHaveText(new RegExp(`^${String((hBefore + 2) % 24).padStart(2, '0')}:`));
  // the night picker's list is drawn dark
  expect(await page.locator('#vdb-night').evaluate(el => getComputedStyle(el).colorScheme)).toBe('dark');
});

test('time picker: keyboard entry, Enter moves Start → End → Set', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 860 });
  await openVdb(page);
  await openWheels(page, '#vdb-time-start');
  const start = page.locator('.tp-side[data-side="start"] b');
  const end = page.locator('.tp-side[data-side="end"] b');

  // "1" waits for a second digit, "9" completes 19 and moves to the minute
  await page.keyboard.type('19');
  await expect(page.locator('.tp-wheel-m')).toBeFocused();
  await page.keyboard.type('45');                       // minute resolution: :45, not :40/:50
  await expect(start).toHaveText('19:45');
  await expect(page.locator('.tp-wheel-m')).toBeFocused(); // waits for Enter

  await page.keyboard.press('Enter');                    // Start → End
  await expect(page.locator('.tp-side[data-side="end"]')).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('.tp-wheel-h')).toBeFocused();
  await page.keyboard.type('2');                         // could be 20-23 …
  await page.keyboard.type('7');                         // … "27" is no hour: ignored
  await page.keyboard.type('3');                         // 23
  await expect(page.locator('.tp-wheel-m')).toBeFocused();
  await page.keyboard.type('7');                         // 6-9 cannot start a minute pair: :07
  await expect(end).toHaveText('23:07');

  // Backspace: minute → hour → back to Start's minute
  await page.keyboard.press('Backspace');
  await expect(page.locator('.tp-wheel-h')).toBeFocused();
  await page.keyboard.press('Backspace');
  await expect(page.locator('.tp-side[data-side="start"]')).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('.tp-wheel-m')).toBeFocused();
  await page.keyboard.press('Enter');                    // Start → End again
  await page.keyboard.type('0230');
  await page.keyboard.press('Enter');                    // End → Set

  await expect(page.locator('.tp-root')).toHaveCount(0);
  await expect(page.locator('#vdb-time-start')).toHaveValue('19:45');
  await expect(page.locator('#vdb-time-end')).toHaveValue('02:30');
});

test('desktop: click a time field and type the time straight in', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 860 });
  await openVdb(page);
  await page.click('#vdb-time-start');
  await expect(page.locator('.tp-is-pop')).toHaveCount(1);    // the wheels come up …
  await expect(page.locator('#vdb-time-start')).toBeFocused(); // … and the field keeps the caret
  await page.keyboard.type('1945');                          // replaces the selected text
  await page.keyboard.press('Tab');
  await expect(page.locator('#vdb-time-start')).toHaveValue('19:45');
  await page.click('#vdb-time-end');
  await page.keyboard.type('230');                           // 02:30
  await page.keyboard.press('Enter');
  await expect(page.locator('#vdb-time-end')).toHaveValue('02:30');
  await expect(page.locator('#vdb-time-start')).toHaveValue('19:45');

  // not a time: the field keeps its value
  await page.click('#vdb-time-end');
  await page.keyboard.type('2399');
  await page.keyboard.press('Enter');
  await expect(page.locator('#vdb-time-end')).toHaveValue('02:30');
});

test('time picker: Escape cancels without changing the times', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 860 });
  await openVdb(page);
  const before = await page.locator('#vdb-time-start').inputValue();
  await openWheels(page, '#vdb-time-start');
  await page.locator('.tp-wheel-h').evaluate(w => { w.scrollTop = 44 * 9; });
  await page.keyboard.press('Escape');
  await expect(page.locator('.tp-root')).toHaveCount(0);
  await expect(page.locator('#vdb-time-start')).toHaveValue(before);
});

// Phones: tapping a (read-only) time field must only open the picker, not
// select its text, which brought up the phone's copy/paste menu.
test.describe('time picker on a touch phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });

  test('tapping a time field selects no text and opens the sheet', async ({ page }) => {
    await openVdb(page);
    await page.tap('#vdb-time-start');
    await expect(page.locator('.tp-is-sheet')).toHaveCount(1);
    const sel = await page.locator('#vdb-time-start').evaluate(el => ({
      start: el.selectionStart, end: el.selectionEnd,
      userSelect: getComputedStyle(el).userSelect || getComputedStyle(el).webkitUserSelect,
      callout: getComputedStyle(el).webkitTouchCallout ?? 'none',
    }));
    expect(sel.end - sel.start, 'no text selected').toBe(0);
    expect(sel.userSelect).toBe('none');
    const prevented = await page.locator('#vdb-time-start').evaluate(el => {
      const ev = new MouseEvent('contextmenu', { bubbles: true, cancelable: true });
      return !el.dispatchEvent(ev);
    });
    expect(prevented, 'long-press text menu suppressed').toBe(true);
  });
});
