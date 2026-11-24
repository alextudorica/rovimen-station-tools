/**
 * Station Admin > Configuration > Overlay: the logo picker.
 *
 * The configuration page is read-only except for this key. #1416 rendered the
 * logo as plain text, so an admin could no longer choose a station's logo;
 * the dropdown lists the images in the station's own assets directory and
 * Save writes through POST /api/overlay/logo/<host>.
 */
import { test, expect } from '@playwright/test';

const ADMIN_USER = 'admin_test';
const PASSWORD = 'test-e2e-pass';
const ASSETS = '/home/rmsuser/rovimen_scripts/assets/';
const LOGOS = ['abtext.png', 'astroian_observatory.png', 'astromania_text.png', 'sternfreunde_berlin.png']
  .map(f => ({ filename: f, path: ASSETS + f }));

async function login(page) {
  await page.goto('/login');
  await page.fill('#login-username', ADMIN_USER);
  await page.fill('#login-password', PASSWORD);
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

test('an admin picks, previews and saves a station logo', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const settings = { overlay: { style: 'cinema', logo: ASSETS + 'astromania_text.png' }, stations: {} };
  let saved = null;
  let previewed = null;
  await page.route('**/api/settings/test_station_1', r => r.fulfill({ json: settings }));
  await page.route('**/api/logo/list/test_station_1', r => r.fulfill({ json: LOGOS }));
  await page.route('**/api/overlay/logo/test_station_1', r => {
    saved = r.request().postDataJSON();
    return r.fulfill({ json: { ok: true, logo: ASSETS + saved.filename } });
  });
  await page.route('**/api/overlay_preview/test_station_1', r => {
    previewed = r.request().postDataJSON();
    return r.fulfill({ status: 500, body: 'no image in the test' });
  });

  await login(page);
  await page.goto('/station/test_station_1/settings');
  await page.getByRole('tab', { name: 'Configuration', exact: true }).click();

  const sel = page.locator('#sa-logo-test_station_1');
  const save = page.locator('#sa-logo-save-test_station_1');
  await expect(sel.locator('option')).toHaveCount(LOGOS.length);
  await expect(sel).toHaveValue('astromania_text.png');
  await expect(save).toBeDisabled();

  await sel.selectOption('astroian_observatory.png');
  await expect(save).toBeEnabled();

  await page.getByRole('button', { name: 'Preview overlay' }).click();
  await expect.poll(() => previewed?.logo).toBe(ASSETS + 'astroian_observatory.png');

  await save.click();
  await expect(page.locator('#sa-logo-msg-test_station_1')).toHaveText(/Saved/);
  expect(saved).toEqual({ filename: 'astroian_observatory.png' });
  await expect(save).toBeDisabled();
  expect(errors).toEqual([]);
});
