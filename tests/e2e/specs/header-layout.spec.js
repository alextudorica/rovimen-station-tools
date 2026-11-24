import { test, expect } from '@playwright/test';

const ADMIN_USER = 'admin_test';
const PASSWORD = 'test-e2e-pass';

async function login(page, user = ADMIN_USER, pass = PASSWORD) {
  await page.goto('/login');
  await page.fill('#login-username', user);
  await page.fill('#login-password', pass);
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

// The shared header (templates/_nav.html) at desktop, tablet and phone widths:
// no page may scroll sideways, every control in the top row shares one height
// and one vertical centre, and there is exactly one menu button. Events and
// Admin used to overflow on phones (their pages never loaded the stylesheet
// that let the header wrap), and Calibration showed two menu buttons
// (dashboard-common.js was evaluated twice under two URLs).
const VIEWPORTS = [
  ['desktop', { width: 1280, height: 800 }],
  ['tablet', { width: 820, height: 1180 }],
  ['phone', { width: 375, height: 667 }],
];
const GUEST_PAGES = ['/', '/events', '/highlights', '/showers'];
const ADMIN_PAGES = ['/events', '/admin', '/calibration', '/station/test_station_1'];

async function headerAudit(page) {
  return page.evaluate(() => {
    const hdr = document.querySelector('.nav-logo').parentElement;
    const shown = e => e.getClientRects().length > 0;
    const row = [...hdr.querySelectorAll('.hc')]
      .filter(e => shown(e) && !e.closest('.nav-panel, .nav-tools-menu, .nav-user-menu'))
      .map(e => e.getBoundingClientRect());
    const centres = row.map(r => r.top + r.height / 2);
    return {
      overflow: document.documentElement.scrollWidth - window.innerWidth,
      heights: [...new Set(row.map(r => Math.round(r.height)))],
      centreSpread: centres.length ? Math.max(...centres) - Math.min(...centres) : 0,
      burgers: document.querySelectorAll('.nav-burger').length,
    };
  });
}

for (const [vpName, vp] of VIEWPORTS) {
  for (const [who, pages] of [['guest', GUEST_PAGES], ['admin', ADMIN_PAGES]]) {
    test(`header fits and aligns: ${who} at ${vpName}`, async ({ page }) => {
      // Night time: the tonight strip then shows its longest wording
      // ("RMS recording · until HH:MM UTC"), which is what overflowed phones.
      await page.clock.setFixedTime(new Date('2026-10-20T20:00:00Z'));
      await page.setViewportSize(vp);
      if (who === 'admin') await login(page);
      for (const path of pages) {
        // 'load', not 'networkidle': the map and detections pages poll.
        await page.goto(path, { waitUntil: 'load' });
        await expect(page.locator('#utc-clock')).toHaveText(/UTC$/);
        await page.waitForTimeout(500);
        const a = await headerAudit(page);
        expect(a.overflow, `${path} scrolls sideways`).toBeLessThanOrEqual(0);
        expect(a.heights, `${path} header controls differ in height`).toHaveLength(1);
        expect(a.centreSpread, `${path} header controls are not centred on one line`).toBeLessThanOrEqual(1);
        expect(a.burgers, `${path} menu button count`).toBe(1);
      }
    });
  }
}

test('phone menu: visitor gets the pages and Log in; Escape closes it', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 667 });
  await page.goto('/events');
  const burger = page.locator('.nav-burger');
  await expect(burger).toBeVisible();
  await expect(page.locator('.main-nav')).toBeHidden();
  await burger.click();
  const panel = page.locator('#nav-panel');
  await expect(panel).toBeVisible();
  await expect(burger).toHaveAttribute('aria-expanded', 'true');
  await expect(panel.locator('.nav-panel-link.active')).toHaveText('Detections');
  await expect(panel.locator('.nav-panel-tools')).toBeHidden();
  await expect(panel.locator('a', { hasText: 'Log in' })).toHaveAttribute('href', /^\/login\?next=%2Fevents/);
  await page.keyboard.press('Escape');
  await expect(panel).toBeHidden();
  await expect(burger).toHaveAttribute('aria-expanded', 'false');
});

test('phone menu: admin gets operator tools and Log out', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 667 });
  await login(page);
  await page.goto('/events');
  await page.click('.nav-burger');
  const panel = page.locator('#nav-panel');
  await expect(panel.locator('.nav-panel-tools')).toBeVisible();
  await expect(panel.locator('.nav-panel-tools a', { hasText: 'Calibration' })).toHaveAttribute('href', '/calibration');
  await expect(panel.locator('a', { hasText: 'Log out' })).toHaveAttribute('href', '/logout');
});

test('desktop: one account control, with Log out in its menu', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await login(page);
  await page.goto('/events');
  await expect(page.locator('.nav-burger')).toBeHidden();
  const user = page.locator('.nav-user-btn');
  await expect(user).toContainText(ADMIN_USER);
  await user.click();
  await expect(page.locator('.nav-user-menu a', { hasText: 'Log out' })).toBeVisible();
});

// The header is sticky, and the tab rows under it stick at its measured
// height (--site-hdr-h). With fixed pixel offsets the station sub-tabs stuck
// 42 px from the top, over the tonight strip, once the header grew.
for (const [vpName, vp] of [['desktop', { width: 1280, height: 800 }], ['phone', { width: 390, height: 844 }]]) {
  test(`scrolled: header stays on top and tab rows stack under it (${vpName})`, async ({ page }) => {
    await page.setViewportSize(vp);
    await login(page);
    for (const [path, tabs] of [['/station/test_station_1/rms', '.sub-tabs'], ['/events', '.ev-tabs'], ['/admin', '.adm-tabs']]) {
      await page.goto(path, { waitUntil: 'load' });
      await expect(page.locator(tabs)).toBeVisible();
      await page.evaluate(() => {
        const pad = document.createElement('div');
        pad.style.height = '3000px';
        document.body.appendChild(pad);
        window.scrollTo(0, 900);
      });
      await page.waitForTimeout(300);
      const r = await page.evaluate((sel) => {
        const hdr = document.querySelector('.nav-logo').parentElement.getBoundingClientRect();
        const t = document.querySelector(sel).getBoundingClientRect();
        return { hdrTop: hdr.top, hdrBottom: hdr.bottom, tabsTop: t.top };
      }, tabs);
      expect(r.hdrTop, `${path}: header scrolled away`).toBe(0);
      expect(Math.abs(r.tabsTop - r.hdrBottom), `${path}: tab row not flush under the header`).toBeLessThanOrEqual(1);
    }
  });
}

// The Video DB controls card (camera, night, time range) scrolls with the
// page; only the header and the station sub-tabs stay pinned.
test('Video DB controls card is not sticky', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await login(page);
  await page.goto('/station/test_station_1/videodb', { waitUntil: 'load' });
  const card = page.locator('.vdb-sticky-header');
  await expect(card).toBeVisible();
  expect(await card.evaluate(el => getComputedStyle(el).position)).toBe('static');
});
