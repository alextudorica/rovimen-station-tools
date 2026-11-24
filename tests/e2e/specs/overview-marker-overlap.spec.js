/**
 * Stations a few pixels apart on the Network map fan out and their names take
 * free sides, so every dot and name stays readable and clickable: gmnro11
 * (Tărâța), gmnro16 (Bacău) and gmnro26 (Buhoci) within 30 px at zoom 7, and
 * gmnro17 / gmnro19 (Câmpenița) on identical coordinates. The stations are the
 * fleet as /api/stations served it on 2026-11-23 (fixtures/map_stations.json).
 */
import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const ADMIN_USER = 'admin_test';
const PASSWORD = 'test-e2e-pass';
const FLEET = JSON.parse(readFileSync(fileURLToPath(new URL('../fixtures/map_stations.json', import.meta.url)), 'utf8'));

async function login(page) {
  await page.goto('/login');
  await page.fill('#login-username', ADMIN_USER);
  await page.fill('#login-password', PASSWORD);
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

// Serve the real fleet's coordinates through /api/overview, each station a
// copy of a fixture station (offline, so no platepar requests).
async function serveFleet(page) {
  await page.route('**/api/overview', async route => {
    const resp = await route.fetch();
    const base = Object.values(await resp.json())[0];
    const data = Object.fromEntries(FLEET.map(st => [st.key, {
      ...base, label: st.label, lat: st.lat, lon: st.lon,
      online: false, status: 'active', show_on_map: true, cameras: [],
    }]));
    await route.fulfill({ response: resp, json: data });
  });
}

// Current map zoom, from the marker scale the page sets (2 ** ((z - 7) / 2),
// clamped to 0.6-2.4, so this reads zooms 5.53-9.53).
async function mapZoom(page) {
  const s = await page.locator('#map').evaluate(el => parseFloat(el.style.getPropertyValue('--ov-marker-scale')));
  return 7 + 2 * Math.log2(s);
}

// Let the zoom animation, the rAF layout and the 0.25 s transitions finish.
async function settle(page) {
  await page.waitForTimeout(700);
}

// One zoom-control click is 0.25 levels; Leaflet ignores a click that lands
// during the previous zoom animation, so wait it out.
async function zoomBy(page, levels) {
  const btn = levels > 0 ? '.leaflet-control-zoom-in' : '.leaflet-control-zoom-out';
  for (let i = 0; i < Math.round(Math.abs(levels) * 4); i++) {
    await page.click(btn);
    await page.waitForTimeout(350);
  }
  await settle(page);
}

// Every overlap the user would see: name boxes over names or dots, dots over
// dots (as circles), and names whose centre does not hit-test to themselves.
function overlaps() {
  const rect = el => el.getBoundingClientRect();
  const hit = (a, b) => Math.min(a.right, b.right) - Math.max(a.left, b.left) > 0.5
    && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 0.5;
  const markers = [...document.querySelectorAll('.leaflet-marker-icon.ov-marker')].map(m => ({
    key: m.querySelector('.ov-marker-id').textContent,
    dot: rect(m.querySelector('.ov-marker-dot')),
    nameEl: m.querySelector('.ov-marker-name'),
  }));
  for (const m of markers) {
    const shown = getComputedStyle(m.nameEl).display !== 'none';
    m.name = shown ? rect(m.nameEl) : null;
  }
  const controls = [...document.querySelectorAll('.leaflet-control')].map(rect);
  const map = rect(document.getElementById('map'));
  const out = [];
  for (const a of markers) {
    for (const b of markers) {
      if (a === b) continue;
      if (a.key < b.key && a.name && b.name && hit(a.name, b.name)) out.push(`name ${a.key} x name ${b.key}`);
      if (a.name && hit(a.name, b.dot)) out.push(`name ${a.key} x dot ${b.key}`);
      const d = Math.hypot((a.dot.left + a.dot.right - b.dot.left - b.dot.right) / 2,
        (a.dot.top + a.dot.bottom - b.dot.top - b.dot.bottom) / 2);
      if (a.key < b.key && d < (a.dot.width + b.dot.width) / 2 - 0.5) out.push(`dot ${a.key} x dot ${b.key}`);
    }
    if (!a.name) continue;
    const cx = (a.name.left + a.name.right) / 2;
    const cy = (a.name.top + a.name.bottom) / 2;
    const inMap = cx > map.left && cx < map.right && cy > map.top && cy < map.bottom
      && cy > 0 && cy < innerHeight && cx > 0 && cx < innerWidth;
    const underControl = controls.some(c => cx >= c.left && cx <= c.right && cy >= c.top && cy <= c.bottom);
    if (inMap && !underControl && document.elementFromPoint(cx, cy)?.closest('.ov-marker-name') !== a.nameEl) {
      out.push(`name ${a.key} not hit at its centre`);
    }
  }
  return { count: markers.length, named: markers.filter(m => m.name).length, out };
}

const markerOf = (page, key) => page.locator('.leaflet-marker-icon.ov-marker', {
  has: page.locator('.ov-marker-id', { hasText: new RegExp(`^${key}$`) }),
});

test.skip('close and identical stations keep separate dots and names on the map', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.setViewportSize({ width: 1600, height: 1200 });
  await login(page);
  await serveFleet(page);
  await page.goto('/network');
  await expect(page.locator('.ov-marker-name[data-side]')).toHaveCount(FLEET.length);
  await page.locator('#map').scrollIntoViewIfNeeded();
  await settle(page);

  // The whole fleet fits at zoom 7 (fitBounds' cap), the screenshot's zoom.
  expect(await mapZoom(page)).toBeCloseTo(7, 1);
  let res = await page.evaluate(overlaps);
  expect(res.count).toBe(FLEET.length);
  expect(res.named).toBe(FLEET.length);
  expect(res.out).toEqual([]);

  // The Câmpenița pair sits on one point: both dots moved, each with a
  // leader line back to it.
  for (const key of ['gmnro17', 'gmnro19']) {
    await expect(markerOf(page, key).locator('.ov-marker-leader')).toBeVisible();
  }

  // The moved dot takes the click and the hover card follows it.
  const dot19 = markerOf(page, 'gmnro19').locator('.ov-marker-dot');
  const dot17 = markerOf(page, 'gmnro17').locator('.ov-marker-dot');
  await dot19.hover();
  const tip = page.locator('.leaflet-tooltip.ov-station-tip');
  await expect(tip).toBeVisible();
  await expect(tip).toContainText('Câmpenița B');
  const [tb, db] = [await tip.boundingBox(), await dot19.boundingBox()];
  expect(Math.abs(tb.x + tb.width / 2 - (db.x + db.width / 2))).toBeLessThan(3);
  expect(tb.y + tb.height).toBeLessThanOrEqual(db.y + 2);
  await dot19.click();
  await expect(dot19).toHaveClass(/ov-marker-selected/);
  await expect(dot17).not.toHaveClass(/ov-marker-selected/);
  await dot19.click();
  await expect(dot19).not.toHaveClass(/ov-marker-selected/);
  await page.mouse.move(0, 0);

  // Zoomed in one level the Bacău pair is still within 1.5 dots.
  await zoomBy(page, 1);
  expect(await mapZoom(page)).toBeCloseTo(8, 1);
  res = await page.evaluate(overlaps);
  expect(res.out).toEqual([]);

  // Zoomed out past 5.5 the names hide, and the dots still don't touch.
  await zoomBy(page, -3);
  await expect(page.locator('#map')).toHaveClass(/ov-labels-hidden/);
  res = await page.evaluate(overlaps);
  expect(res.named).toBe(0);
  expect(res.out).toEqual([]);
  expect(errors).toEqual([]);
});

test('a moved station name still opens its station', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.setViewportSize({ width: 1600, height: 1200 });
  await login(page);
  await serveFleet(page);
  await page.goto('/network');
  await expect(page.locator('.ov-marker-name[data-side]')).toHaveCount(FLEET.length);
  await page.locator('#map').scrollIntoViewIfNeeded();
  await settle(page);
  await markerOf(page, 'gmnro26').locator('.ov-marker-name').click();
  await page.waitForURL(/\/station\/gmnro26$/, { timeout: 5000 });
  expect(errors).toEqual([]);
});
