import { test, expect } from '@playwright/test';

// Final Data Products shows one night: every product (timelapse, sky dome,
// night composite, colour meteor stack, RMS plots) is that night's, and the
// "What this night produced" grid says which ones reached the archive. The
// arrows and the date move the night. Every test asserts zero pageerror.

const ADMIN_USER = 'admin_test';
const PASSWORD = 'test-e2e-pass';
const HOST = 'test_station_1';
const NIGHT = '20260910';

const FAKE_PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=',
  'base64');

async function login(page) {
  await page.goto('/login');
  await page.fill('#login-username', ADMIN_USER);
  await page.fill('#login-password', PASSWORD);
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'),
    { timeout: 10000, waitUntil: 'commit' });
}

for (const [name, size] of [['desktop', { width: 1280, height: 900 }], ['phone', { width: 375, height: 800 }]]) {
  test(`one night for every product, with what it produced (${name})`, async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.setViewportSize(size);
    const plotsCalls = [];
    await page.route('**/api/rms/plots-latest/**', route => {
      const url = new URL(route.request().url());
      const cam = url.pathname.split('/').pop();
      const date = url.searchParams.get('date');
      plotsCalls.push(date);
      route.fulfill({ json: cam === 'TST001'
        ? { date, plots: [{ filename: `X_${date}_radiants.png`, label: 'Radiants' }] }
        : { date: null, plots: [] } });
    });
    // TST002 has no colour meteor stack that night
    await page.route('**/api/rms/plot_image/**', route => route.request().url().includes('TST002') && route.request().url().includes('color_meteor')
      ? route.fulfill({ status: 404, body: '' })
      : route.fulfill({ body: FAKE_PNG, contentType: 'image/png' }));
    await page.route('**/api/sky_dome_timelapse/**/dates', route => route.fulfill({ json: { dates: [] } }));

    await login(page);
    await page.goto(`/station/${HOST}/fdp?date=${NIGHT}`);
    await expect(page.locator('#pane-fdp h1')).toHaveText(/Night of 10 September/, { timeout: 10000 });
    expect(plotsCalls).toContain(NIGHT);
    const comp = page.locator(`#fdp-cam-${HOST}-TST001 [data-kind="comp"] img`);
    await expect(comp).toHaveAttribute('src', new RegExp(`/${NIGHT}/__night_composite__`));
    await expect(page.locator(`#fdp-have-${HOST}-TST001-plots`)).toHaveText('1 plot');
    await expect(page.locator(`#fdp-have-${HOST}-TST001-comp`)).toHaveText('Yes');
    await expect(page.locator(`#fdp-have-${HOST}-TST002-stack`)).toHaveText('None');
    await expect(page.locator(`#fdp-cam-${HOST}-TST002 [data-kind="stack"] .fdp-empty`)).toHaveText('None');
    await expect(page.locator(`#fdp-dome-${HOST}`)).toContainText('No sky dome for this night');
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);

    // the previous night arrow moves every product to the night before
    await page.getByRole('button', { name: 'Previous night' }).click();
    await expect(page.locator('#pane-fdp h1')).toHaveText(/Night of 9 September/);
    await expect(page.locator(`#fdp-night-input-${HOST}`)).toHaveValue('2026-09-09');
    expect(plotsCalls).toContain('20260909');

    expect(errors).toEqual([]);
  });
}
