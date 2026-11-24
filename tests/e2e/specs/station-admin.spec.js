import { test, expect } from '@playwright/test';

// Station Admin (renderSysAdmin): every sub-tab renders from mocked station
// data without a page error or sideways scroll, the night timeline draws one
// lane per camera with its capture gap, and the read-only config shows values
// instead of inputs.
const H = 'test_station_1';
const json = body => route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });

const STATUS = {
  online: true,
  services: { 'rms-cam1': 'active', 'rms-cam2': 'failed', 'color-capture': 'active' },
  storage: { color_capture: { bytes: 4.1e11, days: 3 } },
  disk: { device: '/dev/nvme0n1p2', used_mb: 38000, total_mb: 117000, pct: 32 },
  segment_health: { TST002: { alarm: 'stalls', stalls: 3, window_min: 60 } },
};
const CONFIG = {
  segment_duration: 20, encode_workers: 1, compression_level: 2,
  disk: { warn_pct: 85, nuclear_pct: 90, extreme_pct: 95 },
  stations: { TST001: { rotate: false }, TST002: { rotate: true } },
};
// TST002 lost 02:14-02:31 (854-871 min after noon).
const NIGHT = { cameras: {
  TST001: { spans: [[245, 1040]], total: 2385, stacked: 2385, reencoded: 0, locked: 23, uploaded: 23, rms_complete: true, morning_done: true,
    stages: { rms_complete: 1085, morning_done: 1170, first_upload: 1110, last_upload: 1175 } },
  TST002: { spans: [[245, 854], [871, 1040]], total: 2334, stacked: 2334, reencoded: 0, locked: 27, uploaded: 11, rms_complete: true, morning_done: false },
} };

async function openAdmin(page) {
  await page.route('**/api/status/all', json({ [H]: STATUS }));
  await page.route(`**/api/status/${H}`, json(STATUS));
  await page.route(`**/api/settings/${H}`, json(CONFIG));
  await page.route(`**/api/station_night/${H}/*`, json(NIGHT));
  await page.route(`**/api/logs/${H}/**`, json({ lines: [
    '2026-10-22 02:14:09,551 ERROR TST002 ffmpeg exited with code 1',
    '2026-10-22 02:31:02,117 INFO TST002 connected',
  ] }));
  await page.goto('/login');
  await page.fill('#login-username', 'admin_test');
  await page.fill('#login-password', 'test-e2e-pass');
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
  await page.goto(`/station/${H}/settings`);
  await expect(page.locator(`#sa-tab-health-${H}`)).toHaveAttribute('aria-selected', 'true');
}

for (const [name, size] of [['desktop', { width: 1280, height: 900 }], ['phone', { width: 375, height: 800 }]]) {
  test(`every Station Admin tab renders on ${name}`, async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.setViewportSize(size);
    await openAdmin(page);

    const lanes = page.locator(`#night-tl-${H} .sa-lane`);
    await expect(lanes).toHaveCount(2);
    await expect(lanes.nth(1).locator('.sa-bar-gap')).toHaveCount(1);
    // stage_times draw RMS, dawn and upload bars only where the station recorded them
    await expect(lanes.nth(0).locator('.sa-bar-proc, .sa-bar-up')).toHaveCount(2);
    await expect(lanes.nth(1).locator('.sa-bar-proc, .sa-bar-up')).toHaveCount(0);
    // each camera's step counts sit under its lane
    await expect(page.locator(`#night-tl-${H} .sa-stages`).nth(1)).toContainText('Clips uploaded 11/27');
    // the timeline box never scrolls vertically (the Now line used to stick out)
    expect(await page.locator('.sa-tl').evaluate(el => el.scrollHeight - el.clientHeight)).toBeLessThanOrEqual(0);
    await expect(page.locator(`#night-sum-${H}`)).toContainText('TST002 lost 17 min of colour video at 02:14');

    for (const tab of ['processes', 'updatelogs', 'livestream']) {
      await page.click(`#sa-tab-${tab}-${H}`);
      await expect(page.locator(`#sa-pane-${tab}-${H}`)).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
    }
    // Config is shown, not edited: the only inputs are the visibility switches.
    await page.click(`#sa-tab-processes-${H}`);
    await expect(page.locator(`#sa-pane-processes-${H} input`)).toHaveCount(3);
    await expect(page.locator(`#sa-pane-processes-${H} .sa-scale [aria-current="true"]`)).toContainText('QP 20');

    await page.click(`#sa-tab-updatelogs-${H}`);
    await expect(page.locator('#logs-output .sa-log-row')).toHaveCount(2);
    await expect(page.locator('#logs-n-error')).toHaveText(' 1');

    expect(errors).toEqual([]);
  });
}

test('a live tile swaps Watch for Stop and back', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await openAdmin(page);
  await page.click(`#sa-tab-livestream-${H}`);
  await page.click('text=Watch TST001');
  const stop = page.locator(`#cam-ctl-${H}_TST001 button`);
  await expect(stop).toHaveText('Stop');
  await stop.click();
  await expect(page.locator('text=Watch TST001')).toBeVisible();
  expect(errors).toEqual([]);
});
