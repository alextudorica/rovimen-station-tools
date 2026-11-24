/**
 * Hiding a false positive (bird, satellite, plane) from the Detection tab.
 *
 * Sorted by magnitude, the brightest "meteors" are often birds and
 * satellites. Any signed-in account hides one with one click: the card leaves
 * the list and a "Hidden. Undo" notice appears. "Show hidden" brings it back,
 * dimmed and marked, with an Unhide control.
 *
 * The listing and the hide/unhide API are mocked with page.route() (the test
 * dashboard's detection index is empty); the mock filters by show_hidden the
 * way the server does. Every test asserts zero pageerror.
 */
import { test, expect } from '@playwright/test';

const ADMIN_USER = 'admin_test';
const PASSWORD = 'test-e2e-pass';
const HOST = 'test_station_1';
const CAM = 'TST001';
const NIGHT = '20261105';

async function login(page) {
  await page.goto('/login');
  await page.fill('#login-username', ADMIN_USER);
  await page.fill('#login-password', PASSWORD);
  await page.click('.login-btn');
  await page.waitForURL(url => !url.pathname.includes('/login'), { timeout: 10000, waitUntil: 'commit' });
}

// Three detections: a bright bird, a meteor and a faint meteor.
const DETS = [
  { time: '21:20:05', mag: -4.0, ff: `FF_${CAM}_${NIGHT}_212000_000_0000001.fits` },
  { time: '22:30:10', mag: -1.0, ff: `FF_${CAM}_${NIGHT}_223000_000_0000002.fits` },
  { time: '23:40:15', mag: 2.0, ff: `FF_${CAM}_${NIGHT}_234000_000_0000003.fits` },
];
const iso = d => `2026-11-05T${d.time}.500`;

function clip(d) {
  const hhmmss = d.time.replace(/:/g, '');
  return {
    filename: `${CAM}_${NIGHT}_${hhmmss.slice(0, 4)}00_color.mkv`, time: d.time, size_mb: 3.2,
    stack: null, stack_subdir: null, locked: true, lock_type: 'detection',
    meteor_time: iso(d), detection_offset_s: 5, reencoded: true, source: 'archive',
  };
}

function det(d) {
  return { cam: CAM, date: NIGHT, ff_file: d.ff, meteor_no: 1, time_utc: iso(d),
           mag_apparent: d.mag, shower: 'SPO', duration_s: 0.6 };
}

async function mockStation(page) {
  const hidden = new Map();   // ff_file -> hide record
  const mark = (item, ff) => (hidden.has(ff) ? { ...item, ...hidden.get(ff) } : item);
  const listed = (url, build) => {
    const show = new URL(url).searchParams.get('show_hidden') === '1';
    return DETS.filter(d => show || !hidden.has(d.ff)).map(d => mark(build(d), d.ff));
  };
  await page.route('**/api/videodb/rmsnights/**', route => route.fulfill({ json: [NIGHT] }));
  await page.route('**/api/videodb/chunks/**', route =>
    route.fulfill({ json: listed(route.request().url(), clip) }));
  await page.route('**/api/videodb/rms-detections/**', route =>
    route.fulfill({ json: { detections: listed(route.request().url(), det), hidden_count: hidden.size } }));
  await page.route('**/api/rms/plots/**', route => route.fulfill({ json: [] }));
  await page.route('**/api/twilight/**', route => route.fulfill({ status: 404, json: {} }));
  const answer = async (route, hide) => {
    const body = route.request().postDataJSON();
    const d = DETS.find(x => x.ff === body.ff_file);
    if (!d) return route.fulfill({ status: 404, json: {} });
    const rec = { hidden: true, hidden_by: ADMIN_USER, hidden_at: '2026-11-22T10:00:00+00:00',
                  hidden_reason: body.reason || null };
    if (hide) hidden.set(d.ff, rec); else hidden.delete(d.ff);
    await route.fulfill({ json: { cam: CAM, ff_file: d.ff, meteor_no: 1, time_utc: iso(d), changed: true,
      ...(hide ? rec : { hidden: false, hidden_by: null, hidden_at: null, hidden_reason: null }) } });
  };
  await page.route('**/api/detections/hide', route => answer(route, true));
  await page.route('**/api/detections/unhide', route => answer(route, false));
  return hidden;
}

test.describe('Hide a false positive on the Detection tab', () => {
  let errors;
  test.beforeEach(async ({ page }) => {
    errors = [];
    page.on('pageerror', e => errors.push(e.message));
    // Start with the toggle off, once per test (a reload must keep the choice).
    await page.addInitScript(() => {
      if (!sessionStorage.getItem('hd-e2e')) {
        localStorage.removeItem('rovimen_show_hidden');
        sessionStorage.setItem('hd-e2e', '1');
      }
    });
  });
  test.afterEach(() => { expect(errors).toEqual([]); });

  test('hide the brightest card, see it again with Show hidden, unhide it', async ({ page }) => {
    const hidden = await mockStation(page);
    await login(page);
    await page.goto(`/station/${HOST}/rms`);
    const area = page.locator(`#rms-chunks-${HOST}-${CAM}`);
    const cards = area.locator('.dt-card');
    await expect(cards).toHaveCount(3, { timeout: 10000 });

    await page.selectOption(`#rms-sort-${HOST}-${CAM}`, 'mag-asc');
    await expect(cards.first()).toContainText('-4.0');

    // One click, no confirmation: the card leaves and Undo is offered.
    await cards.first().locator('.hd-btn').click();
    await expect(cards).toHaveCount(2);
    await expect(cards.first()).toContainText('-1.0');
    await expect(page.locator('.hd-undo')).toContainText('Hidden.');
    expect(hidden.size).toBe(1);

    // Show hidden: it is back, dimmed, with who hid it, and the toggle counts it.
    const toggle = page.locator(`#rms-hidden-${HOST}-${CAM}`);
    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-pressed', 'true');
    await expect(cards).toHaveCount(3);
    await expect(toggle).toHaveText('Show hidden (1)');
    const bird = cards.first();
    await expect(bird).toHaveClass(/hd-hidden/);
    await expect(bird).toContainText(`Hidden by ${ADMIN_USER}, 2026-11-22`);
    await expect(bird.locator('.hd-btn')).toHaveText('Unhide');

    // Unhide: the card is a normal card again.
    await bird.locator('.hd-btn').click();
    await expect(cards.first()).not.toHaveClass(/hd-hidden/);
    await expect(cards.first().locator('.hd-btn')).toHaveText('Hide');
    expect(hidden.size).toBe(0);

    // The choice is remembered by the browser.
    await page.reload();
    await expect(page.locator(`#rms-hidden-${HOST}-${CAM}`)).toHaveAttribute('aria-pressed', 'true', { timeout: 10000 });
  });

  test('Undo brings the card straight back', async ({ page }) => {
    const hidden = await mockStation(page);
    await login(page);
    await page.goto(`/station/${HOST}/rms`);
    const cards = page.locator(`#rms-chunks-${HOST}-${CAM} .dt-card`);
    await expect(cards).toHaveCount(3, { timeout: 10000 });
    await page.selectOption(`#rms-sort-${HOST}-${CAM}`, 'mag-asc');
    await cards.first().locator('.hd-btn').click();
    await expect(cards).toHaveCount(2);
    await page.locator('.hd-undo-btn').click();
    await expect(cards).toHaveCount(3);
    await expect(cards.first()).toContainText('-4.0');
    expect(hidden.size).toBe(0);
  });
});

// ── Every list of detections: the toggle in its toolbar, Hide on each card,
//    Hide in the player ──────────────────────────────────────────────────────
//
// The operator could not find "Show hidden" after sorting every meteor by
// magnitude: that is the Events page's Sort & Filter tab, which hides the
// shared Night bar the toggle sat in. Each list now has the toggle in its own
// toolbar (signed-in sessions that may hide only), each detection card has
// Hide, and each player of a detection clip has it. The listings are mocked
// with page.route(); nothing here hides anything, so the store stays empty.

const EV_DATE = '20261105';
const hms = t => t.replace(/:/g, '');
const evDet = (host, cam, time, extra = {}) => ({
  host_key: host, station_label: host === 'test_station_1' ? 'Test Station Alpha' : 'Test Station Beta',
  cam, filename: `${cam}_${EV_DATE}_${hms(time)}_color.mkv`, stack: null,
  meteor_time: `2026-11-05T${time}`, detection_offset_s: 5, pending: false, no_clip: false, pruned: false,
  ...extra,
});
const BIRD = evDet('test_station_1', 'TST001', '21:20:05', { rms: { mag_apparent: -4.0, shower: 'SPO', duration_s: 0.6 } });
const REAL = evDet('test_station_1', 'TST001', '22:30:10', { rms: { mag_apparent: 1.0, shower: 'ORI', duration_s: 0.4 } });
const WITNESS = evDet('test_station_2', 'TST003', '21:20:05', { rms: { mag_apparent: -2.0, shower: 'SPO', duration_s: 0.5 } });

function nightPayload() {
  return {
    date: EV_DATE, correlation_window_s: 1, hidden_count: 0,
    by_station: {
      test_station_1: { label: 'Test Station Alpha', online: true, cameras: { TST001: [BIRD, REAL] } },
      test_station_2: { label: 'Test Station Beta', online: true, cameras: { TST003: [WITNESS] } },
    },
    detections: [BIRD, REAL, WITNESS],
    events: [{ event_time: BIRD.meteor_time, witness_count: 2, station_count: 2, witnesses: [BIRD, WITNESS] }],
  };
}

const DET_GRID = {
  date: EV_DATE, bucket_min: 5, hidden_count: 2,
  columns: [{ cam: 'RO0017', host_key: 'test_station_1', station_label: 'Test Station Alpha' }],
  rows: [{
    bucket_start_min: 21 * 60 + 20, label: '21:20', total: 1, station_count: 1, has_multi: false,
    cells: { RO0017: [{ ...evDet('test_station_1', 'RO0017', '21:20:05'), lock_type: 'detection' }] },
  }],
};

/** Mock the Events page's lists; returns the URLs each list was fetched with. */
async function mockEvents(page) {
  const urls = { night: [], range: [], grid: [] };
  // Most recently registered route wins: the catch-all first.
  await page.route('**/api/detections/**', r => r.fulfill({ json: { date: EV_DATE, detections: [], events: [] } }));
  await page.route('**/api/detections/nights', r => r.fulfill({ json: [EV_DATE] }));
  await page.route(/\/api\/detections\/\d{8}(\?|$)/, r => {
    urls.night.push(r.request().url());
    return r.fulfill({ json: nightPayload() });
  });
  await page.route('**/api/detections/range**', r => {
    urls.range.push(r.request().url());
    return r.fulfill({ json: { detections: [REAL, BIRD].map(d => ({ ...d, date: EV_DATE })), hidden_count: 3 } });
  });
  await page.route('**/api/detections/grid/**', r => {
    urls.grid.push(r.request().url());
    return r.fulfill({ json: DET_GRID });
  });
  await page.route('**/api/footage/grid/**', r => r.fulfill({ json: {
    date: EV_DATE, bucket_min: 1, max_window_min: 5, degraded: false, columns: [], rows: [] } }));
  await page.route('**/api/platepar/**', r => r.fulfill({ json: {} }));
  await page.route('**/api/twilight/**', r => r.fulfill({ status: 404, json: {} }));
  await page.route('**/api/cached-video/**', r =>
    r.fulfill({ status: 200, contentType: 'video/mp4', body: Buffer.alloc(0) }));
  await page.route('**/stack/**', r => r.fulfill({ status: 404, body: '' }));
  return urls;
}

test.describe('Show hidden and Hide on every list of detections', () => {
  let errors;
  test.beforeEach(async ({ page }) => {
    errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.addInitScript(() => {
      if (!sessionStorage.getItem('hd-e2e')) {
        localStorage.removeItem('rovimen_show_hidden');
        sessionStorage.setItem('hd-e2e', '1');
      }
    });
  });
  test.afterEach(() => { expect(errors).toEqual([]); });

  test('Events, All Detections and Multi-Station Events: the toggle in the Night bar, Hide on cards and players', async ({ page }) => {
    await mockEvents(page);
    await login(page);
    await page.goto('/events');
    const toggle = page.locator('.ev-filter-bar #ev-hidden-toggle');
    await expect(toggle).toBeVisible({ timeout: 10000 });
    await expect(toggle).toHaveText('Show hidden');
    await expect(toggle).toHaveAttribute('aria-pressed', 'false');

    const cards = page.locator('#detections-main .ev-card');
    await expect(cards).toHaveCount(3, { timeout: 8000 });
    await expect(cards.locator('.hd-btn')).toHaveCount(3);
    await cards.first().locator('.ev-card-overlay').click();
    await expect(page.locator('#ev-single-modal-container .vm-hide-btn')).toBeVisible();
    await expect(page.locator('#ev-single-modal-container .vm-hide-btn')).toHaveText('Hide');
    await page.keyboard.press('Escape');
    await expect(page.locator('#ev-single-modal-container .vm-backdrop.open')).toHaveCount(0);

    await page.click('#tab-btn-events');
    await expect(toggle).toBeVisible();
    const ev = page.locator('#events-main .ev-event');
    await expect(ev).toHaveCount(1);
    // One Hide per camera of the event.
    await expect(ev.locator('.hd-btn')).toHaveCount(2);
    await ev.locator('.ev-event-time').click();
    await expect(page.locator('.mdm-backdrop')).toBeVisible({ timeout: 3000 });
    await expect(page.locator('.mdm-backdrop .mdm-cap-hide')).toHaveCount(2);
  });

  test('Events, Sort & Filter sorted by magnitude: its own toolbar has the toggle, cards and the player have Hide', async ({ page }) => {
    const urls = await mockEvents(page);
    await login(page);
    await page.goto('/events');
    await expect(page.locator('#ev-hidden-toggle')).toBeVisible({ timeout: 10000 });
    await page.click('#tab-btn-sortfilter');
    // The shared Night bar is hidden on this tab; the toggle is in the tab's toolbar.
    await expect(page.locator('.ev-filter-bar')).toBeHidden();
    const toggle = page.locator('.sf-toolbar #sf-hidden-toggle');
    await expect(toggle).toBeVisible();
    await expect(toggle).toHaveText('Show hidden (3)');
    await expect(toggle).toHaveClass(/grid-btn/);

    await page.selectOption('#sf-sort', 'mag_bright');
    const cards = page.locator('#sortfilter-main .sf-card');
    await expect(cards).toHaveCount(2);
    await expect(cards.first()).toContainText('-4.0');
    await expect(cards.locator('.hd-btn')).toHaveCount(2);
    await cards.first().locator('.sf-card-time').click();
    await expect(page.locator('#ev-single-modal-container .vm-hide-btn')).toBeVisible();
    await page.keyboard.press('Escape');

    // A flip asks every loaded list again, with show_hidden=1.
    const before = urls.range.length;
    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-pressed', 'true');
    await expect.poll(() => urls.range.length).toBeGreaterThan(before);
    expect(urls.range.at(-1)).toContain('show_hidden=1');
    // The other tabs' toggles follow the same choice.
    await expect(page.locator('#ev-hidden-toggle')).toHaveAttribute('aria-pressed', 'true');
  });

  test('Events, Grid: the toggle in its toolbar in Detections only, none for All footage; Hide on thumbs and in the player', async ({ page }) => {
    await mockEvents(page);
    await login(page);
    await page.goto('/events');
    await expect(page.locator('#ev-hidden-toggle')).toBeVisible({ timeout: 10000 });
    await page.click('#tab-btn-grid');
    // All footage lists every chunk: nothing to show, so no toggle anywhere.
    await expect(page.locator('#grid-mode')).toHaveValue('footage');
    await expect(page.locator('#grid-hidden-toggle')).toBeHidden();
    await expect(page.locator('#ev-hidden-toggle')).toBeHidden();

    await page.selectOption('#grid-mode', 'detections');
    const toggle = page.locator('.grid-toolbar #grid-hidden-toggle');
    await expect(toggle).toBeVisible();
    await expect(toggle).toHaveText('Show hidden (2)');
    const thumb = page.locator('#grid-main .grid-thumb');
    await expect(thumb).toHaveCount(1);
    await expect(thumb.locator('.hd-btn')).toHaveCount(1);
    await thumb.click({ position: { x: 8, y: 8 } });
    await expect(page.locator('#ev-single-modal-container .vm-hide-btn')).toBeVisible();
  });

  test('station Archive: the toggle in the camera toolbar, Hide on a clip and in the archive player', async ({ page }) => {
    const nightsUrls = [];
    await page.route('**/api/archive/nights_full/**', r => {
      nightsUrls.push(r.request().url());
      return r.fulfill({ json: [
        { date: NIGHT, timelapse: `${CAM}_${NIGHT}_timelapse.mp4`, timelapse_stack: null, timelapse_size_mb: null,
          hidden_count: 1,
          meteors: DETS.map(d => ({ ...clip(d), stack: null, stack_subdir: null })) },
      ] });
    });
    await page.route('**/api/archive/rms-detections/**', r => r.fulfill({ json: { detections: DETS.map(det) } }));
    await page.route('**/api/archive/file/**', r => r.fulfill({ status: 200, contentType: 'video/mp4', body: Buffer.alloc(0) }));
    await page.route('**/api/videodb/rmsnights/**', r => r.fulfill({ json: [] }));
    await login(page);
    await page.goto(`/station/${HOST}/archive`);
    const toggle = page.locator('.arc-top #arc-hidden-toggle');
    await expect(toggle).toBeVisible({ timeout: 10000 });
    await expect(toggle).toHaveText('Show hidden (1)');

    const clips = page.locator(`#archive-night-sec-${CAM}-${NIGHT} .arc-card:not(.arc-tl)`);
    await expect(clips).toHaveCount(3);
    await expect(clips.locator('.hd-btn')).toHaveCount(3);
    // The timelapse is not a detection.
    await expect(page.locator('.arc-tl .hd-btn')).toHaveCount(0);
    await clips.first().locator('.arc-play').click();
    await expect(page.locator('#arc-modal-container .vm-hide-btn')).toBeVisible();
    await page.keyboard.press('Escape');

    const before = nightsUrls.length;
    await toggle.click();
    await expect.poll(() => nightsUrls.length).toBeGreaterThan(before);
    expect(nightsUrls.at(-1)).toContain('show_hidden=1');
    await expect(page.locator('#arc-hidden-toggle')).toHaveAttribute('aria-pressed', 'true');
  });

  test('station Video DB (footage): Hide on a detection card and in the player, a hidden one dimmed with Unhide, no toggle', async ({ page }) => {
    const chunks = [
      { ...clip(DETS[0]), source: 'station', reencoded: false },
      { ...clip(DETS[1]), source: 'station', reencoded: false, hidden: true, hidden_by: ADMIN_USER,
        hidden_at: '2026-11-22T10:00:00+00:00' },
      { filename: `${CAM}_${NIGHT}_230000_color.mkv`, time: '23:00:00', size_mb: 3, stack: null,
        locked: false, lock_type: null, meteor_time: null, reencoded: false, source: 'station' },
    ];
    await page.route('**/api/videodb/nights/**', r => r.fulfill({ json: [NIGHT] }));
    await page.route('**/api/videodb/chunks/**', r => r.fulfill({ json: chunks }));
    await page.route('**/api/videodb/rms-detections/**', r => r.fulfill({ json: { detections: [] } }));
    await page.route('**/api/cached-video/**', r => r.fulfill({ status: 200, contentType: 'video/mp4', body: Buffer.alloc(0) }));
    await page.route('**/api/cached-files/**', r => r.fulfill({ json: [] }));
    await page.route('**/api/archive/indexed/**', r => r.fulfill({ json: [] }));
    await page.route('**/api/platepar/**', r => r.fulfill({ json: {} }));
    await page.route('**/api/videodb/rmsnights/**', r => r.fulfill({ json: [] }));
    await login(page);
    await page.goto(`/station/${HOST}/videodb`);
    await expect(page.locator('#vdb-results .vdb-chunk-card')).toHaveCount(3, { timeout: 10000 });
    const card = c => page.locator(`#vdb-results .vdb-chunk-card[data-filename="${c.filename}"]`);
    await expect(card(chunks[0]).locator('.hd-btn')).toHaveText('Hide');
    await expect(card(chunks[1])).toHaveClass(/hd-hidden/);
    await expect(card(chunks[1]).locator('.hd-btn')).toHaveText('Unhide');
    // Plain footage carries no Hide, and a footage list has no toggle.
    await expect(card(chunks[2]).locator('.hd-btn')).toHaveCount(0);
    await expect(page.locator('#pane-videodb [data-hd-toggle]')).toHaveCount(0);
    await card(chunks[0]).locator('.vdb-chunk-nostack').click();
    await expect(page.locator('#vdb-modal-container .vm-hide-btn')).toBeVisible();
  });

  test('anonymous visitors get neither the toggle nor Hide', async ({ page }) => {
    await mockEvents(page);
    await page.goto('/events');
    await expect(page.locator('#detections-main .ev-card')).toHaveCount(3, { timeout: 10000 });
    await expect(page.locator('[data-hd-toggle]')).toHaveCount(0);
    await expect(page.locator('.hd-btn')).toHaveCount(0);
    await expect(page.locator('#ev-hidden-slot')).toBeHidden();
  });

  /** Answer hide / unhide by camera and second, as the server does for a
   *  card that knows only its meteor time. */
  async function mockHideApi(page) {
    const posted = [];
    const answer = async (route, hide) => {
      const body = route.request().postDataJSON();
      posted.push({ hide, ...body });
      await route.fulfill({ json: { cam: body.cam, time_utc: body.time_utc, changed: true,
        ...(hide ? { hidden: true, hidden_by: ADMIN_USER, hidden_at: '2026-11-22T10:00:00+00:00', hidden_reason: null }
                 : { hidden: false, hidden_by: null, hidden_at: null, hidden_reason: null }) } });
    };
    await page.route('**/api/detections/hide', r => answer(r, true));
    await page.route('**/api/detections/unhide', r => answer(r, false));
    return posted;
  }

  test('hiding from the magnitude-sorted Sort & Filter list: the card leaves, the count follows, Undo brings it back', async ({ page }) => {
    await mockEvents(page);
    const posted = await mockHideApi(page);
    await login(page);
    await page.goto('/events');
    await expect(page.locator('#ev-hidden-toggle')).toBeVisible({ timeout: 10000 });
    await page.click('#tab-btn-sortfilter');
    await page.selectOption('#sf-sort', 'mag_bright');
    const cards = page.locator('#sortfilter-main .sf-card');
    await expect(cards).toHaveCount(2);
    await cards.first().hover();
    await cards.first().locator('.hd-btn').click();
    await expect(cards).toHaveCount(1);
    await expect(cards.first()).toContainText('+1.0');
    await expect(page.locator('#sf-hidden-toggle')).toHaveText('Show hidden (4)');
    expect(posted[0]).toMatchObject({ hide: true, cam: 'TST001', time_utc: '2026-11-05T21:20:05', page: 'events' });
    await page.locator('.hd-undo-btn').click();
    await expect(cards).toHaveCount(2);
    await expect(page.locator('#sf-hidden-toggle')).toHaveText('Show hidden (3)');
  });

  test('hiding on the Archive tab and the Video DB', async ({ page }) => {
    const posted = await mockHideApi(page);
    await page.route('**/api/archive/nights_full/**', r => r.fulfill({ json: [
      { date: NIGHT, timelapse: null, timelapse_stack: null, timelapse_size_mb: null,
        meteors: DETS.map(d => ({ ...clip(d), stack: null, stack_subdir: null })) },
    ] }));
    const chunks = DETS.map(d => ({ ...clip(d), source: 'station', reencoded: false }));
    await page.route('**/api/videodb/nights/**', r => r.fulfill({ json: [NIGHT] }));
    await page.route('**/api/videodb/chunks/**', r => r.fulfill({ json: chunks }));
    await page.route('**/api/videodb/rms-detections/**', r => r.fulfill({ json: { detections: [] } }));
    await page.route('**/api/videodb/rmsnights/**', r => r.fulfill({ json: [] }));
    await page.route('**/api/cached-files/**', r => r.fulfill({ json: [] }));
    await page.route('**/api/archive/indexed/**', r => r.fulfill({ json: [] }));
    await login(page);

    // Archive: the card leaves (Show hidden is off) and the toggle counts it.
    await page.goto(`/station/${HOST}/archive`);
    const clips = page.locator(`#archive-night-sec-${CAM}-${NIGHT} .arc-card`);
    await expect(clips).toHaveCount(3, { timeout: 10000 });
    await clips.first().locator('.hd-btn').click();
    await expect(clips).toHaveCount(2);
    await expect(page.locator('#arc-hidden-toggle')).toHaveText('Show hidden (1)');
    await expect(page.locator('#arc-title')).toHaveText('2 meteor clips over 1 night');
    await page.locator('.hd-undo-btn').click();
    await expect(clips).toHaveCount(3);
    expect(posted.map(p => p.page)).toEqual(['archive', 'archive']);

    // Video DB (footage): the card stays, dimmed, and its button turns to Unhide.
    await page.goto(`/station/${HOST}/videodb`);
    const card = page.locator(`#vdb-results .vdb-chunk-card[data-filename="${chunks[0].filename}"]`);
    await expect(card.locator('.hd-btn')).toHaveText('Hide', { timeout: 10000 });
    await card.locator('.hd-btn').click();
    await expect(card).toHaveClass(/hd-hidden/);
    await expect(card.locator('.hd-btn')).toHaveText('Unhide');
    await expect(page.locator('#vdb-results .vdb-chunk-card')).toHaveCount(3);
    expect(posted.at(-1)).toMatchObject({ hide: true, cam: CAM, page: 'video' });
  });
});
