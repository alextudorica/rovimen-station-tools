import { test, expect } from '@playwright/test';

// The build guide marks its hero up as <header> and its reading column as
// <main>. dashboard.css styles both bare element selectors for the app shell:
// `header` is the sticky navigation bar (display:flex, position:sticky, top:0,
// z-index:1001) and `main` is the centred content column (max-width:1500px
// plus its own padding). Without an explicit reset the hero inherited the nav
// bar's rules, which squeezed the title, lede, callout and photo figure into
// narrow side-by-side flex columns and pinned the whole block over the
// sections as the reader scrolled. These tests pin the reset in place, since a
// new bare-element rule in dashboard.css would silently reintroduce it.

const ADMIN_USER = 'admin_test';
const PASSWORD = 'test-e2e-pass';

async function login(page) {
  await page.goto('/login');
  await page.fill('#login-username', ADMIN_USER);
  await page.fill('#login-password', PASSWORD);
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

for (const { lang, path } of [{ lang: 'EN', path: '/wiki' }, { lang: 'RO', path: '/wiki/ro' }]) {
  test.describe(`build guide layout (${lang})`, () => {
    test(`hero is a normal block, not the sticky app header`, async ({ page }) => {
      const errors = [];
      page.on('pageerror', e => errors.push(e.message));
      await login(page);
      await page.setViewportSize({ width: 1440, height: 900 });
      await page.goto(path);

      const hero = page.locator('header.wk-hero');
      await expect(hero).toBeVisible();

      const shell = await hero.evaluate(el => {
        const s = getComputedStyle(el);
        return { position: s.position, display: s.display };
      });
      expect(shell.position).toBe('static');
      expect(shell.display).toBe('block');

      expect(errors).toEqual([]);
    });

    test(`hero children stack full width instead of squeezing into columns`, async ({ page }) => {
      await login(page);
      await page.setViewportSize({ width: 1440, height: 900 });
      await page.goto(path);

      const column = await page.locator('main.wk-main').evaluate(el => el.getBoundingClientRect().width);
      const h1 = await page.locator('header.wk-hero h1').evaluate(el => el.getBoundingClientRect().width);
      const figure = await page.locator('header.wk-hero figure').evaluate(el => el.getBoundingClientRect().width);

      // As flex items the five hero children each collapsed to roughly a
      // fifth of the column and wrapped one word per line. Stacked as blocks
      // they each span it.
      expect(column).toBeGreaterThan(600);
      expect(h1).toBeGreaterThan(column * 0.9);
      expect(figure).toBeGreaterThan(column * 0.9);
    });

    test(`hero does not paint over the sections below it`, async ({ page }) => {
      await login(page);
      await page.setViewportSize({ width: 1440, height: 900 });
      await page.goto(path);
      await page.evaluate(() => window.scrollTo(0, 1500));
      await page.waitForFunction(() => window.scrollY > 900);

      // The shell's z-index:1001 put the pinned hero above everything, so the
      // scrolled-to prose was hidden behind it. Hit-test the middle of the
      // reading column: whatever is on top there must not be hero content.
      const covered = await page.evaluate(() => {
        const col = document.querySelector('main.wk-main').getBoundingClientRect();
        const hit = document.elementFromPoint(col.left + col.width / 2, 300);
        return hit ? !!hit.closest('.wk-hero') : false;
      });
      expect(covered).toBe(false);
    });

    test(`hero scrolls away with the page`, async ({ page }) => {
      await login(page);
      await page.setViewportSize({ width: 1440, height: 900 });
      await page.goto(path);

      const before = await page.locator('header.wk-hero h1').evaluate(el => el.getBoundingClientRect().top);
      await page.evaluate(() => window.scrollTo(0, 1200));
      await page.waitForFunction(() => window.scrollY > 600);
      const after = await page.locator('header.wk-hero h1').evaluate(el => el.getBoundingClientRect().top);

      // A sticky hero would hold its viewport position; a block one moves up
      // by the scroll distance.
      expect(before - after).toBeGreaterThan(600);
    });

    test(`modifier classes outrank the reading column's element defaults`, async ({ page }) => {
      await login(page);
      await page.setViewportSize({ width: 1440, height: 900 });
      await page.goto(path);

      // The .wk-main element defaults are wrapped in :where() so single-class
      // modifiers apply as written. Spot-check the three that lost before: the
      // active language pill (was blue-on-blue, an unreadable empty oval), the
      // lede's muted grey, and the cost total's green.
      const styles = await page.evaluate(() => {
        const of = (sel, ...props) => {
          const el = document.querySelector(sel);
          if (!el) return null;
          const s = getComputedStyle(el);
          return Object.fromEntries(props.map(p => [p, s[p]]));
        };
        return {
          pillOn: of('.wk-lang-on', 'color', 'backgroundColor'),
          lede: of('.wk-lede', 'color'),
          body: of('#intro p', 'color'),
          total: of('.wk-cost-total', 'color', 'marginTop'),
        };
      });

      expect(styles.pillOn).not.toBeNull();
      expect(styles.pillOn.color).not.toBe(styles.pillOn.backgroundColor);
      expect(styles.lede.color).not.toBe(styles.body.color);
      if (styles.total) {
        expect(styles.total.color).toBe('rgb(63, 185, 80)');
        expect(styles.total.marginTop).toBe('12px');
      }
    });

    test(`the page does not scroll sideways on a phone`, async ({ page }) => {
      await login(page);
      await page.setViewportSize({ width: 390, height: 844 });
      await page.goto(path);

      // Below 820px .wk-layout flips to a column. It has to switch back to
      // align-items:stretch at the same time, or the children shrink-to-fit
      // their widest content and the prose is clipped off the right edge.
      const m = await page.evaluate(() => ({
        viewport: document.documentElement.clientWidth,
        doc: document.documentElement.scrollWidth,
        column: document.querySelector('main.wk-main').getBoundingClientRect().width,
      }));
      expect(m.doc).toBeLessThanOrEqual(m.viewport);
      expect(m.column).toBeLessThanOrEqual(m.viewport);

      // Wide tables are allowed to overflow, but only inside their own
      // horizontal scroller.
      const escaped = await page.evaluate(() =>
        [...document.querySelectorAll('.wk-table')].some(t => {
          const w = t.closest('.wk-table-wrap');
          return !w || getComputedStyle(w).overflowX !== 'auto';
        }));
      expect(escaped).toBe(false);
    });
  });
}
