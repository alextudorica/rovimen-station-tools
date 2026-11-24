import { test, expect } from '@playwright/test';

// The Showers page is a year calendar drawn into <main id="sh-page-wrap">:
// one row per shower on a shared Jan–Dec axis. The shared skip-link code
// once renamed every <main> to #main, which left the page empty
// ("0 showers") with no error at all.

const MDC = {
  SPO: { name: 'Sporadic' },
  QUA: { name: 'Quadrantids', peak: 'Jan 1', activity_begin: 'Dec 20', activity_end: 'Jan 12', zhr: 120, vg: 40.2 },
  ACE: { name: 'alpha Centaurids', peak: 'Feb 8', activity_begin: 'Jan 31', activity_end: 'Feb 20', zhr: 6 },
  PER: { name: 'Perseids', peak: 'Aug 13', activity_begin: 'Jul 17', activity_end: 'Sep 18', zhr: 100, parent: '109P/Swift-Tuttle',
         sol_begin: 115.0, sol_end: 175.0, sol_peak: 140.1, ra: 47.9, dec: 57.9, vg: 58.8, iau_no: 7, wikipedia: 'Perseids' },
  AAA: { name: 'April alpha-Antliids', peak: 'Apr 20', activity_begin: 'Apr 14', activity_end: 'Apr 23' },
};

// A measured GMN shape for the Perseids only (solar longitude, share of max).
const PROFILES = { years: [2022, 2023, 2024, 2025], meteors: 2458591, showers: {
  PER: { n: 105022, peak_sol: 140.25, peak_share: 0.68,
         profile: [[130.25, 0.1, 0.02], [135.25, 0.4, 0.05], [140.25, 1.0, 0.15], [142.25, 0.5, 0.08], [145.25, 0.1, 0.02]] } },
  // summed ZHR: a 100 +- 14 maximum at the Perseids, 5 elsewhere
  total: { showers: ['PER', 'QUA'],
           series: Array.from({ length: 720 }, (_, b) => {
             const sol = b * 0.5 + 0.25;
             return [sol, Math.abs(sol - 140.25) < 0.1 ? 100 : 5, Math.abs(sol - 140.25) < 0.1 ? 14 : 1];
           }) } };

async function mock(page) {
  await page.route('**/api/showers', r => r.fulfill({ json: MDC }));
  await page.route('**/api/shower-year-counts', r => r.fulfill({ json: { PER: 1234, SPO: 50 } }));
  await page.route('**/api/shower-profiles', r => r.fulfill({ json: PROFILES }));
}

test('Showers: major showers in calendar order, all IAU on demand, search reaches minor ones', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.clock.install({ time: new Date('2026-08-10T21:00:00Z') });
  await mock(page);
  await page.goto('/showers', { waitUntil: 'load' });

  // Major = an IMO rate, ROVIMEN detections, or year-round: AAA is minor.
  await expect(page.locator('#sh-count')).toHaveText('4 showers');
  await expect(page.locator('.sh-row:visible')).toHaveCount(4);
  await expect(page.locator('#sh-n-major')).toHaveText('4');
  await expect(page.locator('#sh-n-all')).toHaveText('5');
  // year-round first, then by peak date
  const codes = await page.locator('.sh-row:visible').evaluateAll(rs => rs.map(r => r.dataset.code));
  expect(codes).toEqual(['SPO', 'QUA', 'ACE', 'PER']);

  // The Quadrantids run across New Year: two bars. The Perseids are active
  // on 10 Aug, with their count formatted.
  await expect(page.locator('.sh-row[data-code="QUA"] .sh-bar')).toHaveCount(2);
  await expect(page.locator('.sh-row[data-code="PER"]')).toHaveClass(/is-active/);
  await expect(page.locator('.sh-row[data-code="PER"] .sh-c-seen')).toHaveText('1,234');
  // the All showers row sums every detection this year, sporadics included
  await expect(page.locator('#sh-total .sh-c-seen')).toHaveText('1,284');
  await expect(page.locator('.sh-row[data-code="PER"]')).toHaveAttribute('href', '#PER');
  await expect(page.locator('#sh-tonight')).toContainText('Active tonight: Perseids.');

  await page.click('.sh-scope-btn[data-scope="all"]');
  await expect(page.locator('#sh-count')).toHaveText('5 showers');
  await expect(page).toHaveURL(/scope=all/);

  await page.click('.sh-scope-btn[data-scope="major"]');
  await page.fill('#sh-search', 'antliid');
  await expect(page.locator('#sh-count')).toHaveText('1 shower');
  await expect(page.locator('.sh-row[data-code="AAA"]')).toBeVisible();

  await page.fill('#sh-search', '');
  await page.check('#sh-only-detections');
  await expect(page.locator('#sh-count')).toHaveText('2 showers');

  // the skip link still lands on the content
  await expect(page.locator('.skip-link')).toHaveAttribute('href', '#sh-page-wrap');
  expect(errors).toEqual([]);
});

test('a row opens its details: dates, Moon, viewing time, measured shape, links', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.clock.install({ time: new Date('2026-10-21T12:00:00Z') });
  await mock(page);
  await page.goto('/showers');
  // the Perseids are drawn from their GMN shape; the others without one get the dashed line
  await expect(page.locator('.sh-row[data-code="PER"] .sh-prof')).toHaveCount(1);
  await expect(page.locator('.sh-row[data-code="QUA"] .sh-bar-noz')).not.toHaveCount(0);
  await expect(page.locator('.sh-axis .sh-moon-in')).toHaveCount(1);
  // the summed ZHR row: mean line, hatched band, tonight's value with its spread
  await expect(page.locator('#sh-total .sh-total-sub')).toHaveText('summed ZHR of 2 showers');
  await expect(page.locator('#sh-total-svg .sh-mean')).toHaveCount(1);
  await expect(page.locator('#sh-total-svg .sh-band')).toHaveAttribute('fill', 'url(#sh-hatch-total)');
  await expect(page.locator('#sh-total-now')).toHaveText('5');
  await expect(page.locator('#sh-total-now')).toHaveAttribute('title', /5 ± 1 \(mean and spread of 4 years\)/);
  await expect(page.locator('.sh-source-note #sh-profile-note')).toBeVisible();

  await page.click('.sh-row[data-code="PER"]');
  const panel = page.locator('#sh-detail');
  await expect(panel).toBeVisible();
  await expect(page).toHaveURL(/\/showers#PER$/);
  await expect(page.locator('.sh-row[data-code="PER"]')).toHaveAttribute('aria-expanded', 'true');
  // 140.1 deg in 2026: 13 August, under the eclipse New Moon
  await expect(panel.locator('.sh-facts')).toContainText('13 Aug 2026');
  await expect(panel.locator('.sh-facts')).toContainText('New Moon');
  await expect(panel.locator('.sh-viewing')).toContainText('Best from Romania on the peak night');
  // the measured mean line with its hatched year-to-year spread
  await expect(panel.locator('.sh-dc .sh-mean')).toHaveCount(1);
  await expect(panel.locator('.sh-dc .sh-band')).toHaveAttribute('fill', 'url(#sh-hatch-dc)');
  await expect(panel.locator('.sh-dc figcaption')).toContainText('105,022 GMN meteors');
  await expect(panel.locator('.sh-dc figcaption')).toContainText('±1 standard deviation');
  await expect(panel.locator('a.sh-detail-go')).toHaveAttribute('href', '/events?shower=PER');
  await expect(panel.locator('a', { hasText: 'Wikipedia' })).toHaveAttribute('href', 'https://en.wikipedia.org/wiki/Perseids');
  await expect(panel.locator('a', { hasText: 'IAU Meteor Data Center' })).toHaveAttribute('href', /kodstrumienia=00007$/);

  // Escape closes it and returns focus to the row; a second click toggles
  await page.keyboard.press('Escape');
  await expect(panel).toHaveCount(0);
  await expect(page.locator('.sh-row[data-code="PER"]')).toBeFocused();
  await expect(page).not.toHaveURL(/#PER/);

  // a shared link opens the shower straight away, even a minor one under "Major"
  await page.goto('/showers#AAA');
  await expect(page.locator('#sh-detail')).toBeVisible();
  await expect(page.locator('.sh-row[data-code="AAA"]')).toBeVisible();
  await expect(page.locator('#sh-detail .sh-dc')).toContainText('No measured activity shape');
  expect(errors).toEqual([]);
});

test('Showers on a phone: no sideways scroll, the year track under each name', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mock(page);
  await page.goto('/showers');
  const row = page.locator('.sh-row[data-code="PER"]');
  await expect(row).toBeVisible();
  const [name, track] = [await row.locator('.sh-name').boundingBox(), await row.locator('.sh-track').boundingBox()];
  expect(track.y).toBeGreaterThan(name.y + name.height - 1);
  expect(track.width).toBeGreaterThan(300);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});

test('About is a header entry that opens the About ROVIMEN panel', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/showers');
  await page.click('.main-nav .nav-about');
  await expect(page.locator('#app-panel')).toHaveAttribute('aria-hidden', 'false');
  await expect(page.locator('#app-panel-title')).toHaveText('About ROVIMEN');
  // the restyled panel: sentence-case sections, the people with their roles
  await expect(page.locator('#app-panel .ap-sec h3')).toHaveText(
    ['Who runs it', 'This website', 'Global Meteor Network', 'Partners', 'The network in numbers']);
  await expect(page.locator('#app-panel .ap-people li')).toHaveCount(2);
  await expect(page.locator('#app-panel .ap-orgs li')).toHaveText(
    ['Astroclubul București', 'Astroian Observatory', 'Sternfreunde Berlin', 'AstroClubul Iași', 'Povești cu Stele', 'Zeta Aquarii Observatory']);
  expect(await page.locator('#app-panel .ap-sec h3').first().evaluate(h => getComputedStyle(h).textTransform)).toBe('none');
});

// Zoom: the slider (and pinch, Ctrl+wheel) narrows the calendar from the year
// to 10 days; dates appear on the axis once they fit, today's in gold; a drag
// pans without following the row's link; "Whole year" resets.
test.describe('Showers calendar zoom', () => {
  async function openAt(page, w, h, touch = false) {
    await page.setViewportSize({ width: w, height: h });
    await page.clock.install({ time: new Date('2026-10-21T12:00:00Z') });
    await page.route('**/api/showers', r => r.fulfill({ json: { ...MDC,
      ORI: { name: 'Orionids', peak: 'Oct 21', activity_begin: 'Oct 2', activity_end: 'Nov 7', zhr: 20 } } }));
    await page.route('**/api/shower-year-counts', r => r.fulfill({ json: { PER: 1234 } }));
    await page.goto('/showers');
    await expect(page.locator('.sh-row').first()).toBeVisible();
  }
  const setZoom = (page, v) => page.evaluate(v => {
    const s = document.getElementById('sh-zoom'); s.value = v; s.dispatchEvent(new Event('input'));
  }, v);

  test('slider: months, then dates; today in gold; drag pans; Whole year resets', async ({ page }) => {
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await openAt(page, 1440, 900);
    await expect(page.locator('.sh-axis .sh-month')).toHaveCount(12);
    await expect(page.locator('.sh-axis .sh-date')).toHaveCount(0);
    await expect(page.locator('.sh-today-label')).toHaveText('Today');
    await expect(page.locator('#sh-zoom-year')).toBeHidden();

    await setZoom(page, 100);   // 10 days around today
    await expect(page.locator('#sh-zoom')).toHaveAttribute('aria-valuetext', '10 days');
    await expect(page.locator('.sh-axis .sh-date.is-today')).toHaveText('21');
    expect(await page.locator('.sh-axis .sh-date').count()).toBeGreaterThanOrEqual(8);
    await expect(page.locator('#sh-cal')).toHaveClass(/show-days/);
    // the Orionids peak (Oct 21) sits inside the visible track
    const [dot, track] = [await page.locator('.sh-row[data-code="ORI"] .sh-peak').boundingBox(),
                          await page.locator('.sh-row[data-code="ORI"] .sh-track').boundingBox()];
    expect(dot.x).toBeGreaterThan(track.x);
    expect(dot.x + dot.width).toBeLessThan(track.x + track.width);
    // nothing on the page scrolls sideways at any zoom
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(1440);

    // drag the Orionids track right: earlier days come in, and the link is not followed
    const before = await page.locator('.sh-axis .sh-date').first().textContent();
    await page.mouse.move(track.x + 200, track.y + track.height / 2);
    await page.mouse.down();
    await page.mouse.move(track.x + 500, track.y + track.height / 2, { steps: 6 });
    await page.mouse.up();
    await expect(page).toHaveURL(/\/showers$/);
    await expect(page.locator('#sh-detail')).toHaveCount(0);   // a drag is not a click
    expect(Number(await page.locator('.sh-axis .sh-date').first().textContent())).toBeLessThan(Number(before));

    await page.click('#sh-zoom-year');
    await expect(page.locator('.sh-axis .sh-month')).toHaveCount(12);
    await expect(page.locator('#sh-zoom')).toHaveValue('0');
    expect(errors).toEqual([]);
  });

  test('phone: a two-finger pinch zooms around the fingers', async ({ page, browserName }) => {
    await openAt(page, 390, 844);
    const track = await page.locator('.sh-row[data-code="ORI"] .sh-track').boundingBox();
    const cdp = await page.context().newCDPSession(page);
    const y = track.y + track.height / 2;
    // fingers around Oct 21 (the 293rd day of 365)
    const cx = track.x + track.width * (293.5 / 365);
    const touch = (type, d) => cdp.send('Input.dispatchTouchEvent', { type,
      touchPoints: type === 'touchEnd' ? [] : [{ x: cx - d, y, id: 1 }, { x: cx + d, y, id: 2 }] });
    await touch('touchStart', 10);
    for (let d = 20; d <= 100; d += 10) await touch('touchMove', d);
    await touch('touchEnd');
    // 20 px apart -> 200 px apart: 10x, so about 36 days, still centred on Oct 21
    await expect(page.locator('#sh-zoom')).not.toHaveValue('0');
    await expect(page.locator('.sh-axis .sh-date.is-today')).toHaveText('21');
    const span = Number((await page.locator('#sh-zoom').getAttribute('aria-valuetext')).split(' ')[0]);
    expect(span).toBeGreaterThan(30);
    expect(span).toBeLessThan(42);
  });
});
