import { test, expect } from '@playwright/test';

// The landing is two pages: "/" is home (hero, numbers, a card pointing to the
// coverage map, Hall of Fame, latest plots as one-row station cards packed by
// length) and "/network" Network overview (coverage numbers, the fleet map,
// live snapshots that load only on request, station status). "/map" and
// "/live" redirect there.

const OV = Object.fromEntries([['gmnro01', 'Ghirdoveni', 6], ['gmnro02', 'Vaslui', 2], ['gmnro03', 'Ciocârlia', 2], ['gmnro07', 'București', 1]]
  .map(([h, label, n]) => [h, {
    online: true, label, lat: 45, lon: 25,
    // Capture state per camera (dashboard/capture_state.py): one stalled.
    cameras: Array.from({ length: n }, (_, k) => ({
      code: `${h.slice(-2)}C${k}`,
      capture: { state: h === 'gmnro07' ? 'stalled' : 'recording', detail: '' },
    })),
    services: Object.fromEntries(Array.from({ length: n }, (_, k) => [`rms-cam${k + 1}`, 'active'])),
  }]));
const STACKS = Object.entries(OV).flatMap(([host, d]) =>
  d.cameras.map(c => ({ host, cam: c.code, date: '20261020', filename: `${c.code}_stack.jpg` })));

async function login(page) {
  await page.goto('/login');
  await page.fill('#login-username', 'admin_test');
  await page.fill('#login-password', 'test-e2e-pass');
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

async function mock(page, liveRequests = []) {
  await page.route('**/api/overview', r => r.fulfill({ json: OV }));
  await page.route('**/api/overview/stacks', r => r.fulfill({ json: STACKS }));
  await page.route('**/api/status/all', r => r.fulfill({ json: {} }));
  await page.route('**/api/rms/plot_image/**', r => r.fulfill({ status: 404, body: '' }));
  await page.route('**/api/latest_ff_maxpixel/**', r => { liveRequests.push(r.request().url()); return r.fulfill({ status: 404, body: '' }); });
}

test('home: hero and latest plots as one-row station cards, longest first, no map', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.setViewportSize({ width: 1280, height: 900 });
  await mock(page);
  await login(page);
  await page.goto('/');
  await expect(page.locator('#ov-hero')).toBeVisible();
  await expect(page.locator('#map')).toHaveCount(0);
  await expect(page.locator('#nw-section')).toHaveCount(0);
  // the coverage card spans the numbers row and leads to the map
  const cover = page.locator('a.ov-cover-card');
  await expect(cover).toHaveAttribute('href', '/network');
  const [cb, tb] = [await cover.boundingBox(), await page.locator('.ov-topbar').boundingBox()];
  expect(Math.abs(cb.x - tb.x)).toBeLessThan(1);
  expect(Math.abs(cb.width - tb.width)).toBeLessThan(1);
  const cards = page.locator('#ov-stacks-grid .ov-station-group');
  await expect(cards).toHaveCount(4);
  // longest first, each station's cameras on one row, short cards sharing a row
  await expect(cards.first()).toContainText('gmnro01');
  const rows = await cards.evaluateAll(els => els.map(e => {
    const tops = [...e.querySelectorAll('.ov-cam-grid > *')].map(c => Math.round(c.getBoundingClientRect().top));
    return { top: Math.round(e.getBoundingClientRect().top), camRows: new Set(tops).size };
  }));
  for (const r of rows) expect(r.camRows, 'cameras on one row').toBe(1);
  expect(new Set(rows.map(r => r.top)).size, 'short cards share a row').toBeLessThan(rows.length);
  expect(errors).toEqual([]);
});

test('home: the card-size slider wraps a station onto more rows instead of scrolling sideways', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.setViewportSize({ width: 1280, height: 900 });
  await mock(page);
  await login(page);
  await page.goto('/');
  const cards = page.locator('#ov-stacks-grid .ov-station-group');
  await expect(cards).toHaveCount(4);
  await page.locator('#ov-zoom').fill('840');
  const first = await cards.first().evaluate(e => {
    const g = e.querySelector('.ov-cam-grid');
    const kids = [...g.children];
    return {
      camRows: new Set(kids.map(c => Math.round(c.getBoundingClientRect().top))).size,
      width: Math.round(kids[0].getBoundingClientRect().width),
      overflow: g.scrollWidth - g.clientWidth,
      pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    };
  });
  expect(first.width).toBeGreaterThan(400);
  expect(first.camRows, 'six cameras wrap').toBeGreaterThan(1);
  expect(first.overflow, 'no sideways scroll in the card').toBeLessThanOrEqual(0);
  expect(first.pageOverflow, 'no sideways scroll on the page').toBeLessThanOrEqual(0);
  expect(errors).toEqual([]);
});

test('Network overview: status table, live snapshots only on request', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const live = [];
  await page.setViewportSize({ width: 1280, height: 900 });
  await mock(page, live);
  await login(page);
  await page.goto('/network');
  await expect(page.locator('.main-nav a.nav-tab.active')).toHaveText('Network overview');
  await expect(page.locator('#nw-section')).toBeVisible();
  await expect(page.locator('#ov-stacks')).toHaveCount(0);
  const btn = page.locator('#ov-live-bar-btn');
  await expect(btn).toHaveText('View live snapshot of all cameras in the fleet');
  await expect(page.locator('#ov-live-bar-note')).toHaveText('10 of 11 cameras recording (1 stalled)');
  await page.waitForTimeout(1000);
  expect(live, 'nothing live is fetched on page load').toEqual([]);

  await btn.click();
  await expect(page.locator('#ov-live')).toBeVisible();
  await expect.poll(() => live.length).toBeGreaterThan(0);
  await expect(btn).toHaveText('Hide the live snapshot');
  expect(errors).toEqual([]);
});

test('/live lands on the live section; /map lands on the Network overview map; Tools no longer lists them', async ({ page }) => {
  await mock(page);
  await login(page);
  await page.goto('/live');
  await expect(page).toHaveURL(/\/network#live$/);
  await page.goto('/map');
  await expect(page).toHaveURL(/\/network$/);
  await expect(page.locator('#map')).toBeVisible();
  await expect(page.locator('.main-nav a.nav-tab.active')).toHaveText('Network overview');
  await expect(page.locator('.nav-tools-menu a[href="/live"], .nav-tools-menu a[href="/network"]')).toHaveCount(0);
});
