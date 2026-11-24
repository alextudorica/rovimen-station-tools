import { test, expect } from '@playwright/test';

const ADMIN_USER = 'admin_test';
const PASSWORD = 'test-e2e-pass';

async function login(page, user = ADMIN_USER) {
  await page.goto('/login');
  await page.fill('#login-username', user);
  await page.fill('#login-password', PASSWORD);
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

async function mockBaselineRoutes(page, { calibrationSummary = { status: 'no_data', date: '20260730' } } = {}) {
  await page.route('**/api/calibration/plot/**', route => route.fulfill({
    json: { status: 'no_data', date: null, expected_date: '20260730', filename: null, overdue: false },
  }));
  await page.route('**/api/latest_stack/**', route => route.fulfill({ status: 404, body: '' }));
  await page.route('**/api/mask_image/**', route => route.fulfill({ status: 404, body: '' }));
  await page.route('**/api/calibration_summary/**', route => route.fulfill({ json: calibrationSummary }));
}

const TINY_PNG = Buffer.from(
  '89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de0000000a49444154789c6360000002000155a2a8d70000000049454e44ae426082',
  'hex',
);

// A 320x240 all-white greyscale PNG. Stands in for an existing mask.bmp:
// the editor takes its canvas resolution from the mask it is replacing, so
// this also pins that the canvas is sized in MASK pixels rather than CSS
// ones. Big enough that one default-radius brush stroke covers a few per
// cent of it rather than the whole frame.
const WHITE_MASK = Buffer.from(
  '89504e470d0a1a0a0000000d4948445200000140000000f008000000005446e2b7000001fe49444154789cedd0410d002010c030c0bfe743c41e84a455b06ccfa238af037e67606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064606460646064607401322a02df37d346740000000049454e44ae426082',
  'hex',
);

/* Fulfil /api/calibration/plot/<kind>/<camera> per camera, so one test can
   hold a fresh camera and a stale one side by side. */
function mockPlots(page, byCamera) {
  return page.route('**/api/calibration/plot/*/*', route => {
    const [, kind, camera] = new URL(route.request().url()).pathname
      .match(/\/api\/calibration\/plot\/([^/]+)\/([^/]+)$/);
    const date = byCamera[camera];
    if (!date) {
      route.fulfill({
        json: { status: 'no_data', date: null, expected_date: '20260730', filename: null, overdue: true },
      });
      return;
    }
    const ext = kind === 'astrometry_report' ? 'jpg' : 'png';
    route.fulfill({
      json: {
        status: 'ok', date, expected_date: '20260730',
        filename: `${camera}_${date}_180000_000000_${kind}.${ext}`,
        overdue: date !== '20260730',
      },
    });
  });
}

test.describe('calibration page', () => {
  test('renders every plot from the archive index, dates each card, and zooms', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));

    // TST001: archive only has a night from two days ago -- stale.
    // TST002: archive has the expected night ready -- fresh.
    await mockPlots(page, { TST001: '20260728', TST002: '20260730' });
    await page.route('**/api/archive/file/**', route => route.fulfill({
      status: 200, contentType: 'image/png', body: TINY_PNG,
    }));
    await page.route('**/api/latest_stack/**', route => route.fulfill({
      status: 200, contentType: 'image/jpeg', body: TINY_PNG,
    }));
    // Real (non-404) mask bytes so the stack-wrap zoom can carry a mask overlay.
    await page.route('**/api/mask_image/**', route => route.fulfill({
      status: 200, contentType: 'image/bmp', body: TINY_PNG,
    }));
    await page.route('**/api/calibration_summary/**', route => route.fulfill({
      json: { status: 'no_data', date: '20260730' },
    }));

    await login(page);
    await page.goto('/calibration');
    await page.waitForSelector('.cal-card');

    const card1 = page.locator('.cal-card[data-cam="TST001"]');

    // All three plots resolve, from the archive, on the stale night.
    for (const kind of ['ff_intervals', 'astrometry_residual', 'astrometry_report']) {
      const img = card1.locator(`.cal-plot-img[data-kind="${kind}"]`);
      await expect(img).toHaveAttribute('src', /\/api\/archive\/file\/TST001\/20260728\/rms\//, { timeout: 10000 });
      await expect(img).toHaveClass(/cal-zoomable/);
    }

    // The freshness line is the page's answer to "how old is this card" --
    // the plots carry no visible date of their own.
    await expect(card1.locator('[data-field="night"]'))
      .toHaveText('Night 2026-07-28 — 2 nights behind', { timeout: 10000 });
    await expect(card1.locator('[data-field="night"]')).toHaveClass(/cal-stale-warn/);

    const card2 = page.locator('.cal-card[data-cam="TST002"]');
    await expect(card2.locator('[data-field="night"]')).toHaveText('Night 2026-07-30', { timeout: 10000 });
    await expect(card2.locator('[data-field="night"]')).not.toHaveClass(/cal-stale/);

    // The platepar-age line is gone for good: it measured the last manual
    // SkyFit solve, which runs to hundreds of days on healthy cameras.
    await expect(card1.locator('[data-field="staleness"]')).toHaveCount(0);
    await expect(page.locator('.cal-card').first()).not.toContainText('Platepar:');

    // Click-to-zoom on a plot: modal opens on that plot's night, no mask overlay.
    await card1.locator('.cal-plot-img[data-kind="astrometry_report"]').click();
    await expect(page.locator('#cal-modal')).toHaveClass(/open/);
    await expect(page.locator('#cal-modal-title')).toContainText('Astrometry calibration');
    await expect(page.locator('#cal-modal-title')).toContainText('2026-07-28');
    await expect(page.locator('#cal-modal-img')).toHaveAttribute('src', /20260728/);
    await expect(page.locator('#cal-modal-dl')).toHaveAttribute('href', /20260728/);
    await expect(page.locator('#cal-modal-mask')).toBeHidden();
    await page.locator('#cal-modal-close').click();
    await expect(page.locator('#cal-modal')).not.toHaveClass(/open/);

    // Click-to-zoom on the stack wrap: mask overlay must carry into the modal.
    // The mask is a plain <div> colorized via CSS mask-image, set to a data:
    // URL of the mask inverted through <canvas> (setupMask in
    // dashboard-calibration.js) -- not an <img src>, and not the raw mask
    // URL, so just assert it resolved to *some* data: URL rather than "none".
    await expect(card1.locator('.cal-mask-img')).not.toHaveCSS('display', 'none', { timeout: 10000 });
    await card1.locator('.cal-stack-wrap').click();
    await expect(page.locator('#cal-modal')).toHaveClass(/open/);
    const modalMask = page.locator('#cal-modal-mask');
    await expect(modalMask).toBeVisible();
    await expect(modalMask).toHaveCSS('mask-image', /^url\("data:/);
    await expect(modalMask).toHaveClass(/cal-mask-tint/);

    expect(errors).toEqual([]);
  });

  test('falls back to the empty state when an archived plot will not load', async ({ page }) => {
    // The index says the plot is there; the bytes do not arrive (a stalled
    // storagebox mount answers 503). Without the fallback the card shows a
    // broken image with its alt text, which reads as a rendering bug.
    await mockPlots(page, { TST001: '20260730', TST002: '20260730' });
    await page.route('**/api/archive/file/**', route => route.fulfill({ status: 503, body: '' }));
    await page.route('**/api/latest_stack/**', route => route.fulfill({ status: 404, body: '' }));
    await page.route('**/api/mask_image/**', route => route.fulfill({ status: 404, body: '' }));
    await page.route('**/api/calibration_summary/**', route => route.fulfill({
      json: { status: 'no_data', date: '20260730' },
    }));

    await login(page);
    await page.goto('/calibration');
    await page.waitForSelector('.cal-card');

    const card = page.locator('.cal-card[data-cam="TST001"]');
    await expect(card.locator('.cal-plot-ff')).toHaveClass(/cal-empty/, { timeout: 10000 });
    await expect(card.locator('.cal-plot-report')).toHaveClass(/cal-empty/, { timeout: 10000 });
    // The night line still reports what the archive knows -- the plot exists,
    // it just could not be fetched.
    await expect(card.locator('[data-field="night"]')).toHaveText('Night 2026-07-30');
  });

  test('shows the empty state when no plot is archived at all', async ({ page }) => {
    await mockPlots(page, {});    // every camera: no_data
    await page.route('**/api/latest_stack/**', route => route.fulfill({
      status: 200, contentType: 'image/jpeg', body: TINY_PNG,
    }));
    await page.route('**/api/mask_image/**', route => route.fulfill({ status: 404, body: '' }));
    await page.route('**/api/calibration_summary/**', route => route.fulfill({
      json: { status: 'no_data', date: '20260730' },
    }));

    await login(page);
    await page.goto('/calibration');
    await page.waitForSelector('.cal-card');

    const card = page.locator('.cal-card').first();
    await expect(card.locator('.cal-plot-ff')).toHaveClass(/cal-empty/, { timeout: 10000 });
    await expect(card.locator('.cal-plot-resid')).toHaveClass(/cal-empty/, { timeout: 10000 });
    await expect(card.locator('.cal-plot-report')).toHaveClass(/cal-empty/, { timeout: 10000 });
    await expect(card.locator('[data-field="night"]')).toHaveText('Night: no plots archived');

    // Stack wrap with no visible mask (404) must zoom without an overlay.
    // setupMask's Image.onerror fires asynchronously, so wait for the mask
    // div to actually confirm it's staying hidden before clicking --
    // otherwise the click can race ahead of it.
    await expect(card.locator('.cal-mask-img')).toHaveCSS('display', 'none', { timeout: 10000 });
    await card.locator('.cal-stack-wrap').click();
    await expect(page.locator('#cal-modal')).toHaveClass(/open/);
    await expect(page.locator('#cal-modal-mask')).toBeHidden();
  });
});

test.describe('calibration page — spectroscopy cameras', () => {
  test('a spectro camera gets no RMS surfaces at all', async ({ page }) => {
    // GH662S/GH662W have no platepar, no RMS config directory and no mask.
    // Every calibration action on them could only ever answer "could not
    // resolve this camera's RMS config directory", so the card says why once
    // instead of offering three buttons that cannot work.
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await mockBaselineRoutes(page);
    await page.route('**/api/stations', async route => {
      const resp = await route.fetch();
      const body = await resp.json();
      const first = Object.keys(body)[0];
      body[first].cameras = [...body[first].cameras, { code: 'GH662S', spectro: true }];
      route.fulfill({ json: body });
    });

    await login(page, ADMIN_USER);
    await page.goto('/calibration');
    await page.waitForSelector('.cal-card');

    const spectro = page.locator('.cal-card[data-cam="GH662S"]');
    await expect(spectro).toHaveClass(/cal-card-spectro/);
    await expect(spectro.locator('[data-field="night"]')).toContainText('Spectroscopy camera');
    await expect(spectro.locator('.cal-plot-wrap')).toHaveCount(0);
    await expect(spectro.locator('.cal-rebase-btn')).toHaveCount(0);
    await expect(spectro.locator('.cal-autofit-btn')).toHaveCount(0);
    await expect(spectro.locator('.cal-mask-btn')).toHaveCount(0);

    // The RMS cameras on the same station are untouched.
    const rms = page.locator('.cal-card:not(.cal-card-spectro)').first();
    await expect(rms.locator('.cal-plot-wrap')).toHaveCount(3);
    expect(errors).toEqual([]);
  });
});

test.describe('calibration page — mask editor', () => {
  test('visitor gets no mask button', async ({ page }) => {
    await mockBaselineRoutes(page);
    await login(page, 'visitor_test');
    await page.goto('/calibration');
    await page.waitForSelector('.cal-card');
    await expect(page.locator('.cal-ops').first()).toBeHidden();
  });

  test('paints a stroke and POSTs a mask the station can use', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await mockBaselineRoutes(page);
    // A 16x16 all-white BMP-as-PNG stands in for an existing mask: the
    // editor sizes itself from it, so the canvas is 16x16 mask pixels.
    await page.route('**/api/mask_image/**', route => route.fulfill({
      status: 200, contentType: 'image/png', body: WHITE_MASK,
    }));
    let posted = null;
    await page.route('**/api/mask/*/*', route => {
      posted = route.request().postDataJSON();
      route.fulfill({ json: { ok: true, width: 1280, height: 720, masked_pct: 3.1 } });
    });

    await login(page, ADMIN_USER);
    await page.goto('/calibration');
    await page.waitForSelector('.cal-card');

    const card = page.locator('.cal-card').first();
    await card.locator('.cal-mask-btn').click();
    await expect(page.locator('#cal-mask-modal')).toHaveClass(/open/);
    // Sized from the mask we served, not from a CSS box.
    await expect(page.locator('#cal-mask-canvas')).toHaveAttribute('width', '320');
    await expect(page.locator('#cal-mask-canvas')).toHaveAttribute('height', '240');
    // Nothing to save until something is drawn.
    await expect(page.locator('#cal-mask-save')).toBeDisabled();

    const canvas = page.locator('#cal-mask-canvas');
    const box = await canvas.boundingBox();
    await page.mouse.move(box.x + box.width * 0.3, box.y + box.height * 0.5);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.7, box.y + box.height * 0.5, { steps: 8 });
    await page.mouse.up();

    await expect(page.locator('#cal-mask-save')).toBeEnabled();
    await expect(page.locator('#cal-mask-stats')).toContainText('unsaved');
    await expect(page.locator('#cal-mask-stats')).not.toContainText('0.0% masked');

    await page.locator('#cal-mask-save').click();
    await expect(page.locator('#cal-mask-status')).toContainText('Saved 1280x720', { timeout: 10000 });
    expect(posted).toBeTruthy();
    expect(typeof posted.png_b64).toBe('string');
    expect(posted.png_b64.length).toBeGreaterThan(0);

    // Saving clears the unsaved marker, so a second Save isn't invited.
    await expect(page.locator('#cal-mask-stats')).not.toContainText('unsaved');
    expect(errors).toEqual([]);
  });

  test('a non-JSON error from the dashboard is reported, not swallowed', async ({ page }) => {
    // What a station running a bundle without /api/mask produces: the
    // dashboard answers with an error page rather than JSON. Parsing that in
    // the same try as the request reported "the save request failed", which
    // names the wrong failure -- the request arrived fine.
    await mockBaselineRoutes(page);
    await page.route('**/api/mask_image/**', route => route.fulfill({
      status: 200, contentType: 'image/png', body: WHITE_MASK,
    }));
    await page.route('**/api/mask/*/*', route => route.fulfill({
      status: 500, contentType: 'text/html', body: '<!doctype html><title>500</title>',
    }));

    await login(page, ADMIN_USER);
    await page.goto('/calibration');
    await page.waitForSelector('.cal-card');
    await page.locator('.cal-card').first().locator('.cal-mask-btn').click();
    await expect(page.locator('#cal-mask-modal')).toHaveClass(/open/);

    const box = await page.locator('#cal-mask-canvas').boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.up();
    await page.locator('#cal-mask-save').click();

    const status = page.locator('#cal-mask-status');
    await expect(status).toContainText('HTTP 500', { timeout: 10000 });
    await expect(status).toContainText('next updater run');
    await expect(status).toHaveClass(/cal-mask-error/);
    // Still saveable: the drawing is not lost to a transient failure.
    await expect(page.locator('#cal-mask-save')).toBeEnabled();
  });

  test('undo restores the frame before the stroke', async ({ page }) => {
    await mockBaselineRoutes(page);
    await page.route('**/api/mask_image/**', route => route.fulfill({
      status: 200, contentType: 'image/png', body: WHITE_MASK,
    }));

    await login(page, ADMIN_USER);
    await page.goto('/calibration');
    await page.waitForSelector('.cal-card');
    await page.locator('.cal-card').first().locator('.cal-mask-btn').click();
    await expect(page.locator('#cal-mask-modal')).toHaveClass(/open/);
    await expect(page.locator('#cal-mask-undo')).toBeDisabled();

    const box = await page.locator('#cal-mask-canvas').boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.up();
    await expect(page.locator('#cal-mask-undo')).toBeEnabled();
    await expect(page.locator('#cal-mask-stats')).not.toContainText('0.0% masked');

    await page.locator('#cal-mask-undo').click();
    await expect(page.locator('#cal-mask-stats')).toContainText('0.0% masked');
  });
});


test.describe('calibration page — platepar auto-fit', () => {
  test('admin sees the auto-fit button; visitor does not', async ({ page }) => {
    await mockBaselineRoutes(page);
    await login(page, 'visitor_test');
    await page.goto('/calibration');
    await page.waitForSelector('.cal-card');
    await expect(page.locator('.cal-ops').first()).toBeHidden();
  });

  test('trigger -> poll -> done -> accept flow', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await mockBaselineRoutes(page);

    let triggered = false;
    let pollCount = 0;
    await page.route('**/api/calibration/autofit/*/*', route => {
      triggered = true;
      route.fulfill({ json: { ok: true, pid: 12345 } });
    });
    await page.route('**/api/calibration/autofit_status/*/*', route => {
      pollCount += 1;
      if (pollCount === 1) {
        route.fulfill({ json: { status: 'running' } });
        return;
      }
      route.fulfill({
        json: {
          status: 'done',
          candidate_id: 'RO000H_20260730_201542',
          accept_token: 'tok_deadbeef',
          best_frame: 'FF_RO000H_20260730_201542_000_0123456.fits',
          n_stars_used: 15,
          n_stars_over_threshold: 2,
          reject_applied: true,
          reject_skipped_reason: null,
          reject_by_quadrant: {},
          arcmin_reject_threshold: 1.5,
          old_residual_arcmin: 2.7,
          old_n_matched: 40,
          new_residual_arcmin: 0.15,
          new_n_matched: 38,
        },
      });
    });
    let acceptedBody = null;
    await page.route('**/api/calibration/autofit_accept/*/*', route => {
      acceptedBody = route.request().postDataJSON();
      route.fulfill({
        json: {
          status: 'applied', applied_at: '2026-07-30T20:16:10Z',
          backup_path: 'platepar_cmn2010.cal.bak_20260730_201610',
          candidate_id: 'RO000H_20260730_201542',
        },
      });
    });

    await login(page, ADMIN_USER);
    await page.goto('/calibration');
    await page.waitForSelector('.cal-card');

    const card = page.locator('.cal-card').first();
    const btn = card.locator('.cal-autofit-btn');
    await expect(btn).toBeVisible();

    await btn.click();
    expect(triggered).toBe(true);

    // First poll: running
    await expect(card.locator('.cal-autofit-status')).toHaveText('Running…', { timeout: 10000 });

    // Second poll: done, with the residual comparison and Accept/Discard
    const result = card.locator('.cal-autofit-result');
    await expect(result).toContainText("2.7' / 40 stars → 0.15' / 38 stars", { timeout: 10000 });
    await expect(result).toContainText('15 stars used');
    await expect(result).toContainText("2 stars over 1.5' dropped");
    await expect(card.locator('.cal-autofit-accept')).toBeVisible();
    await expect(card.locator('.cal-autofit-discard')).toBeVisible();

    await card.locator('.cal-autofit-accept').click();
    await expect(result).toContainText('Applied', { timeout: 10000 });
    expect(acceptedBody).toEqual({
      candidate_id: 'RO000H_20260730_201542', accept_token: 'tok_deadbeef',
    });

    expect(errors).toEqual([]);
  });

  test('re-base reports the pointing shift and accepts', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await mockBaselineRoutes(page);

    let rebaseCalled = false;
    await page.route('**/api/calibration/rebase_pointing/*/*', route => {
      rebaseCalled = true;
      route.fulfill({ json: { ok: true, pid: 4242, mode: 'rebase' } });
    });
    await page.route('**/api/calibration/autofit_status/*/*', route => route.fulfill({
      json: {
        status: 'done', mode: 'rebase',
        candidate_id: 'RO000M_20260921_140000', accept_token: 'tok_rebase',
        source_night: '20260920', n_frames_total: 238, n_frames_recalibrated: 238,
        pointing_shift_arcmin: 37.7, rotation_shift_arcmin: 0.9,
        scale_change_pct: -0.06, distortion_preserved: true,
      },
    }));
    let acceptedBody = null;
    await page.route('**/api/calibration/autofit_accept/*/*', route => {
      acceptedBody = route.request().postDataJSON();
      route.fulfill({ json: { status: 'applied' } });
    });

    await login(page, ADMIN_USER);
    await page.goto('/calibration');
    await page.waitForSelector('.cal-card');

    const card = page.locator('.cal-card').first();
    await card.locator('.cal-rebase-btn').click();
    expect(rebaseCalled).toBe(true);

    const result = card.locator('.cal-autofit-result');
    // A re-base has no residual to compare -- it runs no fit -- so it reports
    // the size of the correction and that the distortion survived.
    await expect(result).toContainText("Pointing moves 37.7'", { timeout: 10000 });
    await expect(result).toContainText('238/238 frames');
    await expect(result).toContainText('distortion unchanged');
    await expect(result).not.toContainText('Residual');

    await card.locator('.cal-autofit-accept').click();
    await expect(result).toContainText('Applied', { timeout: 10000 });
    expect(acceptedBody).toEqual({
      candidate_id: 'RO000M_20260921_140000', accept_token: 'tok_rebase',
    });
    expect(errors).toEqual([]);
  });

  test('discard hides the result without calling accept', async ({ page }) => {
    await mockBaselineRoutes(page);
    let acceptCalled = false;
    await page.route('**/api/calibration/autofit/*/*', route => route.fulfill({ json: { ok: true, pid: 1 } }));
    await page.route('**/api/calibration/autofit_status/*/*', route => route.fulfill({
      json: {
        status: 'done', candidate_id: 'RO000H_20260730_201542', accept_token: 'tok_deadbeef',
        best_frame: 'x',
        n_stars_used: 10, n_stars_over_threshold: 0, reject_applied: false,
        reject_skipped_reason: null, reject_by_quadrant: {},
        arcmin_reject_threshold: 1.5,
        old_residual_arcmin: 1.0, old_n_matched: 12,
        new_residual_arcmin: 0.5, new_n_matched: 12,
      },
    }));
    await page.route('**/api/calibration/autofit_accept/*/*', route => {
      acceptCalled = true;
      route.fulfill({ json: { status: 'applied' } });
    });

    await login(page, ADMIN_USER);
    await page.goto('/calibration');
    await page.waitForSelector('.cal-card');

    const card = page.locator('.cal-card').first();
    await card.locator('.cal-autofit-btn').click();
    await expect(card.locator('.cal-autofit-discard')).toBeVisible({ timeout: 10000 });
    await card.locator('.cal-autofit-discard').click();

    await expect(card.locator('.cal-autofit-result')).toBeHidden();
    expect(acceptCalled).toBe(false);
  });

  test('fit_failed status renders as an error, not stuck on running', async ({ page }) => {
    await mockBaselineRoutes(page);
    await page.route('**/api/calibration/autofit/*/*', route => route.fulfill({ json: { ok: true, pid: 1 } }));
    await page.route('**/api/calibration/autofit_status/*/*', route => route.fulfill({
      json: { status: 'fit_failed', detail: 'autoFitPlatepar could not solve a platepar' },
    }));

    await login(page, ADMIN_USER);
    await page.goto('/calibration');
    await page.waitForSelector('.cal-card');

    const card = page.locator('.cal-card').first();
    await card.locator('.cal-autofit-btn').click();
    await expect(card.locator('.cal-autofit-error')).toContainText('fit_failed', { timeout: 10000 });
    await expect(card.locator('.cal-autofit-error')).toContainText('could not solve a platepar');
  });

  test('a crashed run reports the error instead of hanging on running', async ({ page }) => {
    await mockBaselineRoutes(page);
    await page.route('**/api/calibration/autofit/*/*', route => route.fulfill({ json: { ok: true, pid: 1 } }));
    await page.route('**/api/calibration/autofit_status/*/*', route => route.fulfill({
      json: {
        status: 'error',
        error: 'the auto-fit run exited without writing a result',
        detail: "ModuleNotFoundError: No module named 'RMS'",
      },
    }));

    await login(page, ADMIN_USER);
    await page.goto('/calibration');
    await page.waitForSelector('.cal-card');

    const card = page.locator('.cal-card').first();
    await card.locator('.cal-autofit-btn').click();
    await expect(card.locator('.cal-autofit-error')).toContainText('ModuleNotFoundError', { timeout: 10000 });
    // The button comes back: a dead run must not take the camera out of
    // service, which is exactly what the old permanent "running" did.
    await expect(card.locator('.cal-autofit-btn')).toBeEnabled();
  });
});
