import { test, expect } from '@playwright/test';

// GMN solves a night 4-15 days after it happened, so the orbits layer must
// open on the newest night upstream actually has — and when the user does
// steer onto a night that is not out yet, say so in plain terms instead of
// the red "no orbits" that also means "nobody of ours saw anything".

const ADMIN_USER = 'admin_test';
const PASSWORD = 'test-e2e-pass';

const LATEST = '2026-08-20';
const UNPUBLISHED = '2026-08-24';

async function login(page, user = ADMIN_USER, pass = PASSWORD) {
  await page.goto('/login');
  await page.fill('#login-username', user);
  await page.fill('#login-password', pass);
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'),
    { timeout: 10000, waitUntil: 'commit' });
}

/** Stub the GMN surface: `latest` is published (and empty of our orbits),
 *  anything newer is not published yet. */
async function stubGmn(page) {
  await page.route('**/api/gmn/latest_date', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ latest_available_date: LATEST }),
  }));
  await page.route('**/api/gmn/multistation/**', route => {
    const date = route.request().url().split('/').pop();
    route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        date, total_upstream: 0, events: [], has_ro_count: 0, de_only_count: 0,
        last_fetched_utc: null,
        latest_available_date: LATEST,
        not_published_yet: date > LATEST,
      }),
    });
  });
  await page.route('**/api/gmn/monthly_count**', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ month: '2026-08', ro_orbit_count: 0 }),
  }));
}

test.describe('GMN orbits layer', () => {
  test.beforeEach(async ({ page }) => {
    await stubGmn(page);
    await login(page);
  });

  test('opens on the newest published night, not on yesterday', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await page.goto('/network');
    await page.click('#ml-gmn');

    await expect(page.locator('#gmn-date-input')).toHaveValue(LATEST);
    await expect(page.locator('#gmn-banner')).toBeVisible();
    expect(errors).toEqual([]);
  });

  test('an unpublished night gets a neutral notice and a way back', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await page.goto('/network');
    await page.click('#ml-gmn');
    await expect(page.locator('#gmn-date-input')).toHaveValue(LATEST);

    await page.fill('#gmn-date-input', UNPUBLISHED);
    const banner = page.locator('#gmn-banner');
    await expect(banner).toContainText('not published this night yet');
    await expect(banner).toContainText(LATEST);
    // Neutral, not the red "try another date" — nothing is wrong here.
    await expect(banner).not.toContainText('try another date');

    // One click goes back to the night that does have data.
    await page.click('#gmn-banner .gmn-jump-btn');
    await expect(page.locator('#gmn-date-input')).toHaveValue(LATEST);
    expect(errors).toEqual([]);
  });

  test('a published night with no orbits of ours still says try another date',
    async ({ page }) => {
      const errors = [];
      page.on('pageerror', e => errors.push(e.message));

      await page.goto('/network');
      await page.click('#ml-gmn');
      await expect(page.locator('#gmn-banner')).toBeVisible();

      // The seeded night is published; the stub gives it no events.
      await expect(page.locator('#gmn-banner')).toContainText('try another date');
      await expect(page.locator('#gmn-banner .gmn-jump-btn')).toHaveCount(0);
      expect(errors).toEqual([]);
    });
});
