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

test.describe('hall of fame', () => {
  test('anonymous landing shows the seeded featured meteor', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await page.goto('/');
    await expect(page).not.toHaveURL(/\/login/);

    // The fixture seeds exactly one entry, so the section unhides itself
    // and renders one card with the caption and period badge.
    await expect(page.locator('#ov-hof')).toBeVisible();
    await expect(page.locator('.ov-hof-card')).toHaveCount(1);
    await expect(page.locator('.ov-hof-caption'))
      .toHaveText('E2E featured fireball');
    await expect(page.locator('.ov-hof-badge')).toHaveText('2026-01');

    expect(errors).toEqual([]);
  });

  test('anonymous /api/hall_of_fame carries no curator identity', async ({ request }) => {
    const resp = await request.get('/api/hall_of_fame');
    expect(resp.status()).toBe(200);
    const { entries } = await resp.json();
    expect(entries).toHaveLength(1);
    expect(entries[0].camera).toBe('TST001');
    expect(entries[0].clip_url).toContain('/media/v1/clip/TST001/');
    expect(entries[0]).not.toHaveProperty('added_by');
  });

  test('landing links the heading and "See all" to the Highlights Hall of Fame', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('#ov-hof')).toBeVisible();
    await expect(page.locator('#ov-hof-heading a')).toHaveAttribute('href', '/highlights#hall-of-fame');
    // One entry fits the landing window, so there is nothing more to see.
    await expect(page.locator('#ov-hof-all')).toBeHidden();
  });

  test('anonymous /highlights shows every featured meteor, read-only', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await page.goto('/highlights#hall-of-fame');
    await expect(page).not.toHaveURL(/\/login/);
    await expect(page.locator('#hall-of-fame')).toBeVisible();
    await expect(page.locator('#hof-gallery .ov-hof-card')).toHaveCount(1);
    await expect(page.locator('#hof-gallery .ov-hof-period-h')).toHaveText('January 2026');
    await expect(page.locator('#hof-gallery .ov-hof-caption')).toHaveText('E2E featured fireball');
    await expect(page.locator('.ov-hof-edit')).toHaveCount(0);
    // Highlights order: Fireball research first, then the Hall of Fame, then the brightest events
    const order = await page.evaluate(() => [...document.querySelectorAll('main > section.hl-section')].map(s => s.id));
    expect(order.slice(0, 3)).toEqual(['hl-research', 'hall-of-fame', 'hl-events']);

    expect(errors).toEqual([]);
  });

  test('admin edits the entry in place on its Highlights card', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await login(page);
    await page.goto('/highlights');
    await expect(page.locator('#hof-gallery .ov-hof-card')).toHaveCount(1);
    await expect(page.locator('.ov-hof-edit-caption')).toHaveValue('E2E featured fireball');
    await expect(page.locator('.ov-hof-edit-period')).toHaveValue('2026-01');
    await expect(page.locator('.ov-hof-edit-del')).toBeVisible();
    // Typing in the controls must not open the clip.
    await page.locator('.ov-hof-edit-caption').click();
    await expect(page.locator('.vm-backdrop.open')).toHaveCount(0);

    expect(errors).toEqual([]);
  });

  test('admin gets the Hall of Fame toggle inside the clip modal (add-from-anywhere)', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await login(page);
    await page.goto('/');
    // Logged-in landing renders the featured card below the map; opening it
    // launches the shared video modal, which must carry the admin toggle.
    await expect(page.locator('.ov-hof-card').first()).toBeVisible();
    await page.locator('.ov-hof-card').first().click();
    const hofBtn = page.locator('#hof-modal-container .vm-hof-btn');
    // The seeded clip is already featured → toggle reads the "in" state.
    await expect(hofBtn).toBeVisible();
    await expect(hofBtn).toHaveText(/Hall of Fame/);

    expect(errors).toEqual([]);
  });

  test('anonymous clip modal never shows the Hall of Fame toggle', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    await page.goto('/');
    await expect(page.locator('.ov-hof-card').first()).toBeVisible();
    await page.locator('.ov-hof-card').first().click();
    await expect(page.locator('#hof-modal-container .vm-hof-btn')).toBeHidden();

    expect(errors).toEqual([]);
  });
});
