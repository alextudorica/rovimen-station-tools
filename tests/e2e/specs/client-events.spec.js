/**
 * The browser reports what the server cannot see: a 504 that nginx served
 * while Flask went on to log a 200, and tab switches that make no request.
 * Both go to POST /api/client-event in batches (dashboard-common.js).
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

test('a 504 and the tabs viewed on Detections reach the client-event beacon', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const events = [];
  await page.route('**/api/client-event', route => {
    events.push(...(route.request().postDataJSON()?.events || []));
    return route.fulfill({ status: 204 });
  });
  await page.route('**/api/detections/nights', route => route.fulfill({ json: ['20260602'] }));
  await page.route(/\/api\/detections\/\d{8}$/, route => route.fulfill({ status: 504, body: 'Gateway Time-out' }));
  await login(page);
  await page.goto('/events');
  await expect(page.locator('#ev-date')).toHaveValue('20260602', { timeout: 8000 });
  await page.waitForTimeout(1500);           // "All Detections" dwell
  await page.click('#tab-btn-events');

  await expect.poll(() => events, { timeout: 15000 }).toEqual(expect.arrayContaining([
    expect.objectContaining({ type: 'failure', path: '/api/detections/20260602', status: 504,
                              reason: 'http', page: '/events' }),
    expect.objectContaining({ type: 'tab', page: '/events', tab: 'detections' }),
    expect.objectContaining({ type: 'tab', page: '/events', tab: 'events' }),
  ]));
  // Paths only: no query strings, and the beacon never reports itself.
  expect(events.every(e => !String(e.path || '').includes('?'))).toBe(true);
  expect(events.some(e => e.path === '/api/client-event')).toBe(false);
  expect(errors).toEqual([]);
});
