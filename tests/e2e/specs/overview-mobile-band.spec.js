/** Phone layout: the FOV controls stay put when the station band opens, and no hover card opens. */
import { test, expect } from '@playwright/test';

test.use({ viewport: { width: 390, height: 800 }, hasTouch: true, isMobile: true });

test.skip('FOV controls do not move when a station is selected; orbits go in the band', async ({ page }) => {
  await page.goto('/login');
  await page.fill('#login-username', 'admin_test');
  await page.fill('#login-password', 'test-e2e-pass');
  await page.click('.login-btn');
  await page.waitForURL(u => !u.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
  await page.goto('/network');
  const dot = page.locator('.ov-marker-dot').first();
  await expect(dot).toBeVisible();
  const ctl = page.locator('.fov-control');
  const before = await ctl.boundingBox();
  await dot.tap();
  await expect(page.locator('#station-band')).toBeVisible();
  const after = await ctl.boundingBox();
  expect(Math.abs(after.y - before.y)).toBeLessThan(2);
  await expect(page.locator('.leaflet-tooltip')).toHaveCount(0);
  await expect(page.locator('.sb-orbits').first()).toContainText('GMN orbits');
});
