import { test, expect } from '@playwright/test';

// The Spectro tab: a per-night gallery of grating-camera events, replacing the
// flat text listing that used to sit on Final Data Products (2026-09-19 — 126
// rows of ids and links on gmnro01, most of them "no reduced spectrum").
//
// Covered here: the tab only exists on a station with a spectro camera, the
// cards render with their pictures, the filters narrow the night, and the
// lightbox opens on the image that was clicked and walks the whole night.
// Every test asserts zero pageerror.

const ADMIN_USER = 'admin_test';
const PASSWORD = 'test-e2e-pass';

// 1x1 PNG, so the thumbnails decode instead of rendering as broken images.
const FAKE_PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=',
  'base64');

const NIGHT = '20260918';
const PAYLOAD = {
  spectro: true,
  partial: false,
  cameras: [{
    cam: 'GH662S',
    nights: [{
      date: NIGHT,
      counts: { events: 2, rms: 1, widefield: 1, detected: 1, spectra: 1 },
      events: [
        {
          id: 'RO000A_20260918_212418', time: '21:24:18', trigger: 'rms',
          source: 'RO000A', state: 'none', score: 1.1,
          files: ['event.json', 'reduction.json', 'spectro.jpg'],
        },
        {
          id: 'WIDE_20260918_235813', time: '23:58:13', trigger: 'widefield',
          source: 'GH662W', state: 'spectrum', score: 9.9,
          files: ['event.json', 'reduction.json', 'spectro.jpg',
                  'spectrum.csv', 'spectrum.png', 'wide.jpg'],
        },
      ],
    }],
  }],
};

async function login(page) {
  await page.goto('/login');
  await page.fill('#login-username', ADMIN_USER);
  await page.fill('#login-password', PASSWORD);
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'),
    { timeout: 10000, waitUntil: 'commit' });
}

// The tab's visibility comes from the registry (a camera with spectro: true),
// not from a probe request, so the fixture station is given one here rather
// than in the shared config — no other spec should have to care.
async function withSpectroStation(page, host = 'test_station_1') {
  await page.route('**/api/stations', async route => {
    const res = await route.fetch();
    const body = await res.json();
    const st = (body.stations || body)[host];
    if (st && Array.isArray(st.cameras)) {
      st.cameras.push({ code: 'GH662S', rotate: false, az: null, alt: null,
                        spectro: true });
    }
    await route.fulfill({ response: res, json: body });
  });
  await page.route('**/api/spectro/**', route =>
    route.fulfill({ json: PAYLOAD }));
  await page.route('**/api/archive/file/**/spectro/**', route =>
    route.fulfill({ body: FAKE_PNG, contentType: 'image/png' }));
}

async function openSpectroTab(page) {
  await expect(page.locator('#tab-btn-spectro')).toBeVisible({ timeout: 10000 });
  await page.click('#tab-btn-spectro');
  await expect(page.locator('#pane-spectro .spectro-card').first())
    .toBeVisible({ timeout: 10000 });
}

test.describe('Spectro tab', () => {
  test('appears only for a station that has a grating camera', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await login(page);
    await withSpectroStation(page);

    await page.goto('/station/test_station_1/rms');
    await expect(page.locator('#tab-btn-spectro')).toBeVisible({ timeout: 10000 });

    await page.goto('/station/test_station_2/rms');
    await expect(page.locator('#tab-btn-spectro')).toBeHidden();

    expect(errors).toEqual([]);
  });

  test('renders one card per event with its pictures and badges', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await login(page);
    await withSpectroStation(page);
    await page.goto('/station/test_station_1/rms');
    await openSpectroTab(page);

    const cards = page.locator('#pane-spectro .spectro-card');
    await expect(cards).toHaveCount(2);
    // The event with a spectrum carries all three pictures; the empty one
    // still gets its grating-camera frame rather than no picture at all.
    await expect(cards.nth(0).locator('.spectro-thumb img')).toHaveCount(1);
    await expect(cards.nth(1).locator('.spectro-thumb img')).toHaveCount(3);
    await expect(cards.nth(1)).toHaveClass(/spectro-state-spectrum/);
    await expect(page.locator('#pane-spectro')).toContainText('spectrum extracted');

    expect(errors).toEqual([]);
  });

  test('filters narrow the night to the events that found something', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await login(page);
    await withSpectroStation(page);
    await page.goto('/station/test_station_1/rms');
    await openSpectroTab(page);

    // .spectro-filter, not text=: the summary chips carry the same words
    // ("1 with a spectrum") and a bare text selector matches those first.
    await page.click('.spectro-filter:has-text("With a spectrum")');
    await expect(page.locator('#pane-spectro .spectro-card')).toHaveCount(1);
    await expect(page.locator('#pane-spectro')).toContainText('WIDE_20260918_235813');

    await page.click('.spectro-filter:has-text("All events")');
    await expect(page.locator('#pane-spectro .spectro-card')).toHaveCount(2);

    expect(errors).toEqual([]);
  });

  test('a thumbnail opens the lightbox on that image and the arrows walk the night',
    async ({ page }) => {
      const errors = [];
      page.on('pageerror', e => errors.push(e.message));
      await login(page);
      await withSpectroStation(page);
      await page.goto('/station/test_station_1/rms');
      await openSpectroTab(page);

      // Second card, first image: the widefield frame that fired the trigger.
      await page.locator('.spectro-card').nth(1).locator('.spectro-thumb img')
        .first().click();
      const modal = page.locator('#spectro-modal');
      await expect(modal).toHaveClass(/open/);
      await expect(page.locator('#spectro-modal-title'))
        .toContainText('Widefield trigger frame');
      await expect(page.locator('#spectro-modal-pos')).toHaveText('2 / 4');

      await page.keyboard.press('ArrowRight');
      await expect(page.locator('#spectro-modal-pos')).toHaveText('3 / 4');
      await expect(page.locator('#spectro-modal-title'))
        .toContainText('Grating camera');

      await page.keyboard.press('Escape');
      await expect(modal).not.toHaveClass(/open/);

      expect(errors).toEqual([]);
    });

  // The reported failure, at the level the operator met it: on
  // gmnro01/GH662S/20260920 the tab asks for 170 pictures at once and 66 of
  // them came back 503 from a saturated storagebox lane (measured against
  // prod, 2026-09-22), so a third of the gallery — the reduced-spectrum plots
  // among them — rendered as broken images that stayed broken until a reload.
  test('a picture the storagebox shed heals itself instead of staying broken',
    async ({ page }) => {
      const errors = [];
      page.on('pageerror', e => errors.push(e.message));
      await login(page);
      await withSpectroStation(page);

      // One 503 on the reduced spectrum, the way a shed lane answers, then the
      // real picture. Registered after withSpectroStation so it wins for this
      // one file.
      let attempts = 0;
      await page.route(/\/spectro\/[^/]*spectrum\.png/, route => {
        attempts += 1;
        if (attempts === 1) {
          return route.fulfill({ status: 503, contentType: 'application/json',
                                 body: '{"error":"storagebox unavailable"}' });
        }
        return route.fulfill({ body: FAKE_PNG, contentType: 'image/png' });
      });

      await page.goto('/station/test_station_1/rms');
      await openSpectroTab(page);

      const plot = page.locator('.spectro-thumb', { hasText: 'Reduced spectrum' })
        .locator('img');
      // The retry is deliberately delayed so the burst that caused the shed has
      // drained, so poll past it rather than asserting on the first paint.
      await expect.poll(() => plot.evaluate(el => el.naturalWidth),
                        { timeout: 20000 }).toBeGreaterThan(0);
      expect(attempts).toBe(2);

      expect(errors).toEqual([]);
    });

  test('a failed listing offers a retry instead of reading as "no events"',
    async ({ page }) => {
      const errors = [];
      page.on('pageerror', e => errors.push(e.message));
      await login(page);
      await withSpectroStation(page);
      await page.route('**/api/spectro/**', route =>
        route.fulfill({ status: 504, body: 'gateway timeout' }));

      await page.goto('/station/test_station_1/rms');
      await expect(page.locator('#tab-btn-spectro')).toBeVisible({ timeout: 10000 });
      await page.click('#tab-btn-spectro');

      await expect(page.locator('#pane-spectro')).toContainText('Could not list');
      await expect(page.locator('#pane-spectro')).toContainText('Retry');

      expect(errors).toEqual([]);
    });
  // dashboard.css styles the bare `header` element as the app's sticky
  // navigation bar at z-index 1001. Each event card's header is a semantic
  // <header>, so every one of a night's cards became a sticky bar ABOVE the
  // z-index 1000 lightbox: opening a plot showed the gallery's headers and
  // badges painted across it. The rule is now scoped to `body > header`.
  test('the gallery does not paint over an open lightbox', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await login(page);
    await withSpectroStation(page);
    await page.goto('/station/test_station_1/rms');
    await openSpectroTab(page);

    // The card header must not have been claimed by the app-header rule.
    const hdr = await page.locator('.spectro-card-hdr').first().evaluate(el => {
      const s = getComputedStyle(el);
      return { position: s.position, zIndex: s.zIndex };
    });
    expect(hdr.position).not.toBe('sticky');
    expect(hdr.zIndex).toBe('auto');

    await page.locator('#pane-spectro .spectro-thumb img').first().click();
    await expect(page.locator('#spectro-modal')).toHaveClass(/open/);

    // Nothing from the page may be hit-testable over the backdrop.
    const intruders = await page.evaluate(() => {
      const found = [];
      for (let fx = 0.1; fx <= 0.9; fx += 0.1) {
        for (let fy = 0.1; fy <= 0.9; fy += 0.1) {
          const el = document.elementFromPoint(
            Math.round(window.innerWidth * fx), Math.round(window.innerHeight * fy));
          if (el && !el.closest('#spectro-modal')) {
            found.push(el.tagName + '.' + String(el.className).split(' ')[0]);
          }
        }
      }
      return [...new Set(found)];
    });
    expect(intruders).toEqual([]);

    expect(errors).toEqual([]);
  });
});
