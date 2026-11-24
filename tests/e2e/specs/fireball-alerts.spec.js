import { test, expect } from '@playwright/test';

// Fireball alerts page (/alerts): reached from the account menu, shows the
// Telegram and Discord join links and the email subscription form; subscribe
// and unsubscribe update the form. The API is intercepted, so no email is sent.

async function login(page) {
  await page.goto('/login');
  await page.fill('#login-username', 'admin_test');
  await page.fill('#login-password', 'test-e2e-pass');
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

test('account menu links to the alerts page; subscribe and unsubscribe', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const calls = [];
  await page.route('**/api/alerts/email', async (route) => {
    const req = route.request();
    calls.push(req.method());
    if (req.method() === 'POST') {
      const { email } = JSON.parse(req.postData());
      await route.fulfill({ json: { alert_email: email, confirmation_sent: true, confirmation_error: '' } });
    } else {
      await route.fulfill({ json: { alert_email: null } });
    }
  });
  await page.setViewportSize({ width: 1280, height: 860 });
  await login(page);
  await page.goto('/alerts');
  // the account menu (rendered by dashboard-common.js on every page) links here
  await page.locator('#nav-auth summary').click();
  await expect(page.locator('#nav-auth a[href="/alerts"]')).toBeVisible();
  await page.keyboard.press('Escape');

  await expect(page.locator('a[href="https://t.me/+e2e"]')).toBeVisible();
  await expect(page.locator('a[href="https://discord.gg/e2e"]')).toBeVisible();

  await page.fill('#al-email', 'member@example.org');
  await page.click('#al-subscribe');
  await expect(page.locator('#al-status')).toContainText('Subscribed as member@example.org');
  await expect(page.locator('#al-unsubscribe')).toBeVisible();

  await page.click('#al-unsubscribe');
  await expect(page.locator('#al-status')).toContainText('Unsubscribed');
  await expect(page.locator('#al-unsubscribe')).toBeHidden();
  expect(calls).toEqual(['POST', 'DELETE']);
  expect(errors).toEqual([]);
});

test('the alerts page needs a signed-in user', async ({ page }) => {
  await page.goto('/alerts');
  await expect(page).toHaveURL(/\/login/);
});
