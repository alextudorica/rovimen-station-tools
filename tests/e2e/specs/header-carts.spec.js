import { test, expect } from '@playwright/test';

// Basket and compilation cart as header icons (#nav-carts) instead of floating
// bottom-right pills that covered content. Each opens a panel: a dropdown under
// the header on desktop, a bottom sheet over a scrim on phones; a
// Basket | Compilation switch jumps between them; a click outside closes.

const ITEMS = ['RO000A', 'RO000H', 'RO000P'].map((cam, i) => ({
  id: `bk_00000000000${i}`, camera: cam, date: '2026-10-20',
  filename: `${cam}_20261020_22${i}000_color.mkv`,
}));
const CART = { clips: ['RO000A', 'RO000H'].map((cam, i) => ({
  host_key: 'test_station_1', cam, date: '20261020', filename: `${cam}_20261020_22${i}000_color.mkv`,
  stack: null, label: 'Test Station Alpha', pre: 3, post: 5,
})) };

async function login(page) {
  await page.goto('/login');
  await page.fill('#login-username', 'admin_test');
  await page.fill('#login-password', 'test-e2e-pass');
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

async function openDetections(page) {
  await page.addInitScript(cart => localStorage.setItem('rovimen_compilation_cart', cart), JSON.stringify(CART));
  await page.route('**/api/detections/**', r => r.fulfill({ json: { date: '20261020', detections: [], events: [] } }));
  await page.route('**/api/detections/nights', r => r.fulfill({ json: ['20261020'] }));
  await page.route('**/api/basket', r => r.fulfill({ json: { items: ITEMS } }));
  await login(page);
  await page.goto('/events');
  await expect(page.locator('#nav-carts #basket-fab')).toBeVisible({ timeout: 5000 });
}

test('desktop: cart icons in the header, dropdown panels with a switch', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.setViewportSize({ width: 1280, height: 860 });
  await openDetections(page);

  await expect(page.locator('#nav-carts #comp-fab')).toBeVisible();
  await expect(page.locator('#basket-fab-badge')).toHaveText('3');
  await expect(page.locator('#comp-fab-count')).toHaveText('2');
  await expect(page.locator('.fab-stack')).toHaveCount(0);

  await page.click('#basket-fab');
  const basket = page.locator('#basket-panel');
  await expect(basket).toBeVisible();
  const box = await basket.boundingBox();
  expect(box.y, 'opens under the header row').toBeGreaterThan(50);
  expect(box.x + box.width, 'inside the viewport').toBeLessThanOrEqual(1280);
  await expect(page.locator('#nav-cart-scrim')).toBeHidden();   // no scrim on desktop

  const sw = basket.locator('.nav-cart-switch-btn');
  await expect(sw).toHaveCount(2);
  await expect(sw.first()).toHaveText(/^Basket\s*3$/);
  await sw.nth(1).click();
  await expect(page.locator('#comp-drawer')).toHaveClass(/\bopen\b/);
  await expect(page.locator('#comp-drawer')).toBeVisible();
  await expect(basket).toBeHidden();

  // a click outside closes it
  await page.mouse.click(200, 600);
  await expect(page.locator('#comp-drawer')).toBeHidden();

  // Escape closes the basket panel
  await page.click('#basket-fab');
  await expect(basket).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(basket).toBeHidden();
  expect(errors).toEqual([]);
});

test('phone: icons beside the menu button, panels as a bottom sheet over a scrim', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openDetections(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
  const [cart, burger] = [await page.locator('#basket-fab').boundingBox(), await page.locator('.nav-burger').boundingBox()];
  expect(Math.abs(cart.y - burger.y), 'same header row as the menu button').toBeLessThanOrEqual(1);

  await page.click('#basket-fab');
  const basket = page.locator('#basket-panel');
  await expect(basket).toBeVisible();
  const box = await basket.boundingBox();
  expect(Math.round(box.y + box.height), 'docked at the bottom').toBe(844);
  expect(box.width).toBe(390);
  await expect(page.locator('#nav-cart-scrim')).toBeVisible();

  await page.mouse.click(195, 60);   // the scrim, above the sheet
  await expect(basket).toBeHidden();
  await expect(page.locator('#nav-cart-scrim')).toBeHidden();
});
