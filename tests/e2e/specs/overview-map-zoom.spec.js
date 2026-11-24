/**
 * The wheel zooms the overview map (#1364); the station dots and their
 * name labels scale with it, and each label stays centred beside its dot.
 */
import { test, expect } from '@playwright/test';

const ADMIN_USER = 'admin_test';
const PASSWORD = 'test-e2e-pass';

async function login(page) {
  await page.goto('/login');
  await page.fill('#login-username', ADMIN_USER);
  await page.fill('#login-password', PASSWORD);
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

async function markerGeometry(page) {
  return page.evaluate(() => {
    const dot = document.querySelector('.ov-marker-dot').getBoundingClientRect();
    const name = document.querySelector('.ov-marker-name').getBoundingClientRect();
    return {
      dotW: dot.width, nameH: name.height,
      dotMidY: dot.top + dot.height / 2, nameMidY: name.top + name.height / 2,
      gap: name.left - dot.right,
    };
  });
}

test('wheel zoom scales the station dots and labels with the map', async ({ page }) => {
  await page.setViewportSize({ width: 1400, height: 1400 });
  await login(page);
  await page.goto('/network');
  const dot = page.locator('.ov-marker-dot').first();
  await expect(dot).toBeVisible();
  await page.waitForTimeout(500);
  const before = await markerGeometry(page);

  // Zoom in around the first station so it stays in view.
  const box = await dot.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  for (let i = 0; i < 6; i++) { await page.mouse.wheel(0, -120); await page.waitForTimeout(60); }
  await page.waitForTimeout(800);
  const after = await markerGeometry(page);

  expect(after.dotW).toBeGreaterThan(before.dotW * 1.5);
  expect(after.nameH).toBeGreaterThan(before.nameH * 1.5);
  // The label stays centred on the dot and just clear of it.
  expect(Math.abs(after.nameMidY - after.dotMidY)).toBeLessThan(2);
  expect(after.gap).toBeGreaterThan(0);
  expect(after.gap).toBeLessThan(8);
});

test('zoomed far out, the dots collapse into a status card', async ({ page }) => {
  await page.setViewportSize({ width: 1400, height: 1400 });
  await login(page);
  await page.goto('/network');
  const dot = page.locator('.ov-marker-dot').first();
  await expect(dot).toBeVisible();
  await expect(page.locator('.ov-marker-name').first()).toBeVisible();
  const box = await page.locator('#map').boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  for (let i = 0; i < 12; i++) { await page.mouse.wheel(0, 120); await page.waitForTimeout(60); }
  await page.waitForTimeout(800);
  await expect(page.locator('.ov-marker-name').first()).toBeHidden();
  await expect(dot).toBeHidden();
  const card = page.locator('.ov-zoomout-card');
  await expect(card).toContainText('online');
  await card.click();
  await expect(dot).toBeVisible();
});
