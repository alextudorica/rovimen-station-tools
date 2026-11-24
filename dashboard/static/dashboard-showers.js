import { openAppPanel, appPanelClose, showerFullName, escHtml } from '/static/dashboard-common.js';
import { solToDate, dayOfYear, jdFromDate, moonPhase, moonPhaseName, nightView } from './dashboard-sky.js';

window.openAppPanel = openAppPanel;
window.appPanelClose = appPanelClose;

/*
 * dashboard-showers.js
 * ────────────────────
 * The Showers page as a year calendar: one row per shower on a shared
 * Jan–Dec axis. A row's bar is the activity period (brighter for a higher
 * ZHR), the dot is the peak, and a line marks today on every row. "Major"
 * (the default) lists showers with an IMO rate or ROVIMEN detections this
 * year; "All IAU" lists the whole IAU Meteor Data Center (about 900).
 * Clicking a row opens its details under it (/showers#PER): this year's
 * dates, the Moon at the peak, when the radiant is best placed from Romania,
 * the measured activity shape, and a link to the ROVIMEN detections. A thin
 * strip under the axis shows the Moon's phase through the year.
 *
 * Zoom: every track holds an inner layer (.sh-track-in) laid out over the
 * whole year at --sh-zoom x the track width and shifted by --sh-pan, both
 * set once on #sh-cal, so zooming 900 rows is two custom properties. The
 * axis is redrawn in pixels on each change: months, then dates once they fit.
 */

const MONTH_ABBR = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const MONTH_FULL = ['January','February','March','April','May','June','July','August','September','October','November','December'];
// Day-of-year (0-based, 365-day year) on which each month starts.
const MONTH_START = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334, 365];
const YEAR_ROUND = new Set(['SPO', 'ANT']);   // sporadic and antihelion: no season
const YEAR = new Date().getUTCFullYear();
// The site the viewing times are for: the centre of the network, as the
// header's tonight strip uses (dashboard-common.js NETWORK_CENTRE).
const SITE = { lat: 45.94, lon: 24.97 };
const TZ = 'Europe/Bucharest';

/** "Aug 9" -> day of year (0..364), or null. */
function doy(s) {
  if (!s) return null;
  const [mon, day] = s.trim().split(/\s+/);
  const mi = MONTH_ABBR.indexOf(mon);
  const d = parseInt(day, 10);
  return mi < 0 || isNaN(d) ? null : MONTH_START[mi] + Math.min(d, MONTH_START[mi + 1] - MONTH_START[mi]) - 1;
}

function todayDoy() {
  const now = new Date();
  return MONTH_START[now.getUTCMonth()] + now.getUTCDate() - 1;
}

const pct = d => `${(d / 365 * 100).toFixed(3)}%`;

/** Activity as [start, end] day ranges; a period across New Year is two. */
function segments(info) {
  const b = doy(info.activity_begin), e = doy(info.activity_end), p = doy(info.peak);
  if (b == null || e == null) return p == null ? [] : [[Math.max(0, p - 1), Math.min(364, p + 1)]];
  return b <= e ? [[b, e]] : [[b, 364], [0, e]];
}

function isActive(info, today) {
  return segments(info).some(([b, e]) => today >= b && today <= e);
}

// Bar brightness follows the rate: ZHR 2 is faint, 100+ is full.
function intensity(zhr) {
  if (zhr == null) return 0.35;
  return Math.min(1, 0.4 + 0.6 * Math.log10(Math.max(zhr, 1)) / 2);
}

const fmtCount = n => n == null ? '' : n.toLocaleString('en-GB');

/* ── Activity profile ─────────────────────────────────────────────────────
   Measured, not modelled: /api/shower-profiles holds each shower's GMN
   multi-station meteors per sporadic meteor, per half day of solar longitude,
   averaged over several years and scaled to 1 at its maximum
   (tools/build_shower_profiles.py). The shape is that ratio; the height is the shower's IMO ZHR on a log scale
   (ZHR 150 fills the row). A shower with a measured shape but no ZHR is an
   outline at a fixed low height: the timing is known, the rate is not. No
   GMN shape: a dashed line over the MDC activity period, never an invented
   curve. Drawn in day units in a viewBox one year wide, so the inner layer's
   zoom stretches it like the bars. */
const PROFILE_H = 30;        // viewBox height (the row's track height)
const PROFILE_BASE = 26;     // the curve's floor, from the top
const PROFILE_MAX = 22;      // the height of a ZHR-150 shower
const PROFILE_NOZ = 8;       // the height of a shape whose rate is unknown
let _profiles = {};          // code -> { n, peak_sol, peak_share, profile: [[sol, rel], ...] }
let _profileMeta = null;     // { years, meteors }
let _total = null;           // { showers: [codes], points: [[doy, mean, sd], ...] } (this year's days)

/** This year's day of year for a J2000 solar longitude. */
const _solDoyCache = new Map();
function solDoy(sol) {
  if (!_solDoyCache.has(sol)) _solDoyCache.set(sol, dayOfYear(solToDate(sol, YEAR)));
  return _solDoyCache.get(sol);
}

function profile(info, gmn) {
  const z = info.zhr;
  const h = z == null ? PROFILE_NOZ : 4 + (PROFILE_MAX - 4) * Math.log10(Math.max(z, 1) + 1) / Math.log10(151);
  const peak = solDoy(gmn.peak_sol);
  // Unwrap days around the peak, so a season across New Year is one shape.
  const xs = gmn.profile.map(([sol, r]) => [peak + (((solDoy(sol) - peak + 182.625) % 365.25 + 365.25) % 365.25) - 182.625, r]);
  const half = 0.25;        // half a bin, in days
  const pts = xs.map(([x, r]) => `${(x + 0.5).toFixed(2)},${(PROFILE_BASE - h * r).toFixed(2)}`);
  const x0 = (xs[0][0] + 0.5 - half).toFixed(2), x1 = (xs[xs.length - 1][0] + 0.5 + half).toFixed(2);
  const d = `M${x0},${PROFILE_BASE} L${pts.join(' L')} L${x1},${PROFILE_BASE} Z`;
  // The same shape a year earlier and later, so a season across New Year
  // shows on both ends of the calendar.
  const path = [0, -365, 365].map(o =>
    `<path d="${d}" transform="translate(${o} 0)" vector-effect="non-scaling-stroke"/>`).join('');
  return {
    svg: `<svg class="sh-prof${z == null ? ' sh-prof-noz' : ''}" viewBox="0 0 365 ${PROFILE_H}" preserveAspectRatio="none" style="--i:${intensity(z).toFixed(2)}">${path}</svg>`,
    peakDoy: ((peak % 365) + 365) % 365,
    peakTop: ((PROFILE_BASE - h) / PROFILE_H * 100).toFixed(1),
  };
}

/* ── Rows ─────────────────────────────────────────────────────────────────── */

function rowHtml(code, info, count, today) {
  const name = info.name || showerFullName(code);
  const yearRound = YEAR_ROUND.has(code);
  const active = !yearRound && isActive(info, today);
  const major = yearRound || info.zhr != null || count > 0;
  const p = doy(info.peak);
  const gmn = !yearRound ? _profiles[code] : null;
  const prof = gmn && gmn.profile && gmn.profile.length ? profile(info, gmn) : null;
  const bars = yearRound
    ? `<span class="sh-bar sh-bar-year"></span>`
    : prof ? prof.svg
    // No rate known: a dashed line along the floor, so it doesn't read as weak.
    : segments(info).map(([b, e]) =>
        `<span class="sh-bar sh-bar-noz" style="left:${pct(b)};width:${pct(e - b + 1)}"></span>`).join('');
  // The dot sits on the measured maximum when there is one.
  const peakAt = prof ? prof.peakDoy : p;
  const peakDot = !yearRound && peakAt != null
    ? `<span class="sh-peak" style="left:${pct(peakAt + 0.5)};top:${prof ? prof.peakTop : PROFILE_BASE / PROFILE_H * 100}%"></span>` : '';
  const when = yearRound ? 'all year'
    : (info.activity_begin && info.activity_end ? `active ${info.activity_begin} to ${info.activity_end}` : '');
  const label = [name, code, when, info.peak ? `peak ${info.peak}` : '',
    info.zhr != null ? `ZHR ${info.zhr}` : '', info.vg != null ? `geocentric speed ${info.vg} km/s` : '',
    info.parent ? `parent ${info.parent}` : '',
    count ? `${fmtCount(count)} ROVIMEN detections in ${YEAR}` : ''].filter(Boolean).join(', ');
  const profNote = gmn && _profileMeta
    ? `Activity shape: ${fmtCount(gmn.n)} GMN meteors, ${_profileMeta.years[0]}–${_profileMeta.years.at(-1)}`
    : 'No measured activity shape';
  return `<a class="sh-row${active ? ' is-active' : ''}${yearRound ? ' is-year-round' : ''}"
      href="#${encodeURIComponent(code)}" aria-expanded="false" aria-controls="sh-detail" data-code="${escHtml(code)}"
      data-name="${escHtml(name.toLowerCase())}" data-count="${count || 0}" ${major ? 'data-major' : ''}
      aria-label="${escHtml(label)}" title="${escHtml((info.parent ? `${name}, from ${info.parent}` : name) + '. ' + profNote)}">
    <span class="sh-name"><span class="sh-name-text">${escHtml(name)}</span> <span class="sh-code" translate="no">${escHtml(code)}</span></span>
    <span class="sh-track" aria-hidden="true"><span class="sh-track-in">${bars}${peakDot}</span></span>
    <span class="sh-num sh-c-peak">${yearRound ? 'all year' : escHtml(info.peak || '')}</span>
    <span class="sh-num sh-c-zhr">${info.zhr != null ? escHtml(String(info.zhr)) : ''}</span>
    <span class="sh-num sh-c-vg">${info.vg != null ? Math.round(info.vg) : ''}</span>
    <span class="sh-num sh-c-seen">${fmtCount(count || null)}</span>
  </a>`;
}

function axisHtml() {
  return `<div class="sh-axis" aria-hidden="true">
    <span class="sh-name"><span class="sh-moon-key">Moon</span></span>
    <span class="sh-track sh-axis-track"><span class="sh-axis-labels" id="sh-axis-track"></span><span class="sh-track-in sh-moon-in"></span></span>
    <span class="sh-num sh-c-peak">Peak</span>
    <span class="sh-num sh-c-zhr" title="Zenithal hourly rate (IMO)">ZHR</span>
    <span class="sh-num sh-c-vg" title="Geocentric speed (Vg)">km/s</span>
    <span class="sh-num sh-c-seen" title="ROVIMEN detections this year">${YEAR}</span>
  </div>`;
}

// The Moon's lit fraction at 22:00 UTC (midnight in Romania) each night of
// the year, as a gradient: dark for a new Moon, pale for a full one.
function moonBand() {
  const stops = [];
  for (let d = 0; d < 365; d++) {
    const f = moonPhase(jdFromDate(new Date(Date.UTC(YEAR, 0, 1 + d, 22)))).fraction;
    const c = `color-mix(in srgb,var(--hdr-text) ${Math.round(f * 70)}%,var(--hdr-ink-2))`;
    stops.push(`${c} ${pct(d)} ${pct(d + 1)}`);
  }
  return `linear-gradient(to right, ${stops.join(', ')})`;
}

/* ── Total: the summed ZHR of every rated shower ─────────────────────────
   From /api/shower-profiles "total": per half day, the mean over the years
   of the summed ZHR curves, and the standard deviation of those yearly
   sums (so showers that vary together are not treated as independent).
   Drawn in pixels on each zoom change, so the hatched uncertainty stays
   crisp at any zoom. */
function totalRowHtml() {
  if (!_total) return '';
  // every ROVIMEN detection this year: all showers and the sporadics
  const seen = Object.values(_counts).reduce((a, n) => a + (Number(n) || 0), 0);
  return `<div class="sh-total" id="sh-total">
    <span class="sh-name"><span class="sh-name-text">All showers</span>
      <span class="sh-total-sub">summed ZHR of ${_total.showers.length} showers</span></span>
    <span class="sh-track sh-total-track" aria-hidden="true"><svg id="sh-total-svg"></svg></span>
    <span class="sh-num sh-c-peak">tonight</span>
    <span class="sh-num sh-c-zhr" id="sh-total-now"></span>
    <span class="sh-num sh-c-vg"></span>
    <span class="sh-num sh-c-seen" title="${seen ? `${fmtCount(seen)} ROVIMEN detections in ${YEAR}, showers and sporadics` : ''}">${fmtCount(seen || null)}</span>
  </div>`;
}

const HATCH = id => `<defs><pattern id="${id}" patternUnits="userSpaceOnUse" width="5" height="5" patternTransform="rotate(45)">
  <line x1="0" y1="0" x2="0" y2="5" class="sh-hatch-line"/></pattern></defs>`;

/** A "nice" axis maximum and step for values up to `v`. */
function niceScale(v) {
  const raw = Math.max(v, 1) / 3;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 5, 10].map(m => m * mag).find(st => st >= raw);
  return { step, max: Math.ceil(v / step) * step };
}

/** Mean line and hatched band (mean +- sd) through `pts` [[x, mean, sd]] in
 *  px, on a y scale `y(v)`. */
function bandPaths(pts, y, hatchId) {
  if (!pts.length) return '';
  const top = pts.map(([x, m, sd]) => `${x.toFixed(1)},${y(m + sd).toFixed(1)}`);
  const bot = pts.map(([x, m, sd]) => `${x.toFixed(1)},${y(Math.max(0, m - sd)).toFixed(1)}`).reverse();
  const line = pts.map(([x, m]) => `${x.toFixed(1)},${y(m).toFixed(1)}`);
  const d = `M${top.join(' L')} L${bot.join(' L')} Z`;
  // a faint wash under the hatching keeps a thin band visible at year scale
  return `<path class="sh-band-wash" d="${d}"/><path class="sh-band" d="${d}" fill="url(#${hatchId})"/>
    <path class="sh-mean" d="M${line.join(' L')}"/>`;
}

function _drawTotal(pxDay) {
  const svg = document.getElementById('sh-total-svg');
  if (!svg || !_total) return;
  const w = svg.parentElement.clientWidth, h = svg.parentElement.clientHeight;
  if (!w || !h) return;
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  const lo = _zoom.start - 1, hi = _zoom.start + _zoom.span + 1;
  // the series a year earlier and later too, for the seam at New Year
  const pts = [];
  for (const off of [-365, 0, 365]) {
    for (const [d, m, sd] of _total.points) if (d + off >= lo && d + off <= hi) pts.push([d + off, m, sd]);
  }
  pts.sort((a, b) => a[0] - b[0]);
  const vmax = Math.max(...pts.map(([, m, sd]) => m + sd), 1);
  const { step, max } = niceScale(vmax);
  const pad = 4;
  const y = v => h - pad - (v / max) * (h - 2 * pad - 8);
  const x = d => (d + 0.5 - _zoom.start) * pxDay;
  let grid = '';
  for (let v = step; v <= max + 1e-9; v += step) {
    grid += `<line class="sh-total-grid" x1="0" x2="${w}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"/>
      <text class="sh-total-tick" x="${w - 4}" y="${(y(v) - 3).toFixed(1)}" text-anchor="end">${v}</text>`;
  }
  const todayX = x(_zoom.today);
  svg.innerHTML = HATCH('sh-hatch-total') + grid +
    bandPaths(pts.map(([d, m, sd]) => [x(d), m, sd]), y, 'sh-hatch-total') +
    (todayX >= 0 && todayX <= w ? `<line class="sh-total-today" x1="${todayX.toFixed(1)}" x2="${todayX.toFixed(1)}" y1="0" y2="${h}"/>` : '');
  const now = _total.points.reduce((best, p) => Math.abs(p[0] - _zoom.today - 0.5) < Math.abs(best[0] - _zoom.today - 0.5) ? p : best);
  const nowEl = document.getElementById('sh-total-now');
  if (nowEl) {
    nowEl.textContent = Math.round(now[1]);
    nowEl.title = `Summed ZHR tonight: ${now[1].toFixed(0)} ± ${now[2].toFixed(0)} (mean and spread of ${_profileMeta.years.length} years)`;
  }
}

// Alternating month bands behind every track, as one gradient.
function monthBands() {
  const stops = [];
  for (let i = 0; i < 12; i++) {
    const c = i % 2 ? 'var(--hdr-ink-2)' : 'var(--hdr-ink)';
    stops.push(`${c} ${pct(MONTH_START[i])} ${pct(MONTH_START[i + 1])}`);
  }
  return `linear-gradient(to right, ${stops.join(', ')})`;
}

/* ── "Tonight" sentence ───────────────────────────────────────────────────── */

function namesList(names) {
  if (names.length <= 1) return names.join('');
  if (names.length <= 4) return `${names.slice(0, -1).join(', ')} and ${names.at(-1)}`;
  return `${names.slice(0, 3).join(', ')} and ${names.length - 3} more`;
}

function tonightText(entries, today) {
  const seasonal = entries.filter(([c, i]) => !YEAR_ROUND.has(c) && i.zhr != null);
  const active = seasonal.filter(([, i]) => isActive(i, today))
    .sort((a, b) => b[1].zhr - a[1].zhr)
    .map(([c, i]) => i.name || showerFullName(c));
  const peaking = seasonal.find(([, i]) => doy(i.peak) === today);
  const upcoming = seasonal
    .map(([c, i]) => ({ c, i, d: ((doy(i.peak) ?? 999) - today + 365) % 365 }))
    .filter(x => x.d > 0 && doy(x.i.peak) != null)
    .sort((a, b) => a.d - b.d)[0];
  const parts = [];
  parts.push(active.length ? `Active tonight: ${namesList(active)}.` : 'No major shower is active tonight.');
  if (peaking) parts.push(`The ${peaking[1].name} peak tonight (ZHR ${peaking[1].zhr}).`);
  if (upcoming) parts.push(`Next peak: ${upcoming.i.name}, ${upcoming.i.peak} (ZHR ${upcoming.i.zhr}).`);
  return parts.join(' ');
}

/* ── Render, filter ──────────────────────────────────────────────────────── */

let _scope = 'major';
let _searchQuery = '';
let _onlyDetections = false;

function render(mdc, counts) {
  const today = todayDoy();
  const cal = document.getElementById('sh-cal');
  cal.style.setProperty('--today', pct(today + 0.5));
  cal.style.setProperty('--bands', monthBands());

  const entries = Object.entries(mdc);
  // Year-round first, then by peak date through the year; no peak last.
  const key = ([c, i]) => YEAR_ROUND.has(c) ? -1 : (doy(i.peak) ?? 999);
  entries.sort((a, b) => key(a) - key(b) || (b[1].zhr ?? -1) - (a[1].zhr ?? -1));

  cal.style.setProperty('--moon', moonBand());
  _mdc = mdc;
  _counts = counts;
  cal.innerHTML = axisHtml() + totalRowHtml() +
    `<div class="sh-rows">${entries.map(([c, i]) => rowHtml(c, i, counts[c] ?? 0, today)).join('')}</div>`;

  const nMajor = cal.querySelectorAll('.sh-row[data-major]').length;
  document.getElementById('sh-n-major').textContent = nMajor;
  document.getElementById('sh-n-all').textContent = entries.length;
  document.getElementById('sh-tonight').textContent = tonightText(entries, today);
  _applyFilter();
  _zoom.today = today;
  _applyZoom();
  _openFromHash();
}

/* ── Details panel ───────────────────────────────────────────────────────── */

let _mdc = {};
let _counts = {};
let _openCode = null;

const fmtDay = d => d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' });
const fmtTime = d => d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: TZ });
const tzName = d => (new Intl.DateTimeFormat('en-GB', { timeZone: TZ, timeZoneName: 'short' })
  .formatToParts(d).find(p => p.type === 'timeZoneName') || {}).value || 'local time';

/** "From Romania on the peak night: ..." */
function viewingText(info, peakAt) {
  if (info.ra == null || info.dec == null) return 'The IAU data gives no radiant position for this shower.';
  const v = nightView(info.ra, info.dec, peakAt, SITE);
  if (!v.dark) return 'There is no dark sky on the peak night at this latitude.';
  if (v.good) {
    const tz = tzName(v.good.from);
    const best = v.best ? `, highest at ${fmtTime(v.best.at)} (${Math.round(v.best.alt)}° up)` : '';
    return `Best from Romania on the peak night: ${fmtTime(v.good.from)}–${fmtTime(v.good.to)} ${tz}, while the radiant is at least 25° up in a dark sky${best}.`;
  }
  if (v.best && v.best.alt > 0) {
    return `From Romania the radiant stays low in the dark hours (at most ${Math.round(v.best.alt)}° up, around ${fmtTime(v.best.at)} ${tzName(v.best.at)}): expect few meteors.`;
  }
  return 'From Romania the radiant is below the horizon while the sky is dark: this shower is not visible from here.';
}

/** The season up close: the measured shape (or the MDC period as a dashed
 *  line), the Moon's lit fraction each night underneath, date ticks and today. */
/** The season up close: the measured mean shape with its year-to-year
 *  spread hatched (or the MDC period as a dashed line), the Moon's lit
 *  fraction each night underneath, date ticks and today. The plot itself is
 *  drawn in pixels by drawSeasonChart() once the panel has a width. */
function seasonRange(code, info) {
  const gmn = _profiles[code];
  if (gmn && gmn.profile && gmn.profile.length) {
    const peakX = solDoy(gmn.peak_sol);
    const pts = gmn.profile.map(([sol, r, sd]) =>
      [peakX + (((solDoy(sol) - peakX + 182.625) % 365.25 + 365.25) % 365.25) - 182.625, r, sd || 0]);
    return { pts, peakX, lo: Math.floor(pts[0][0] - 2), hi: Math.ceil(pts.at(-1)[0] + 2), gmn };
  }
  const b = doy(info.activity_begin), e = doy(info.activity_end), p = doy(info.peak);
  if (b == null || e == null || p == null) return null;
  return { pts: null, peakX: p, lo: Math.floor(p - ((p - b + 365) % 365) - 2), hi: Math.ceil(p + ((e - p + 365) % 365) + 2) };
}

function seasonChart(code, info) {
  const r = seasonRange(code, info);
  if (!r) return '';
  const { lo, hi } = r;
  const span = hi - lo;
  const X = d => ((d - lo) / span * 100);
  // One cell per night for the Moon, and date ticks every few days.
  let moon = '', ticks = '';
  const tickStep = span > 60 ? 10 : span > 30 ? 5 : span > 14 ? 2 : 1;
  for (let d = lo; d < hi; d++) {
    const date = new Date(Date.UTC(YEAR, 0, 1 + d, 22));
    const f = moonPhase(jdFromDate(date)).fraction;
    moon += `<span style="left:${X(d).toFixed(3)}%;width:${(100 / span).toFixed(3)}%;--f:${Math.round(f * 70)}%" title="${fmtDay(date)}: Moon ${Math.round(f * 100)}% lit"></span>`;
    if (date.getUTCDate() === 1 || (date.getUTCDate() - 1) % tickStep === 0 && date.getUTCDate() < 30) {
      ticks += `<span style="left:${X(d + 0.5).toFixed(3)}%">${date.getUTCDate() === 1 || d === lo ? fmtDay(date) : date.getUTCDate()}</span>`;
    }
  }
  const note = r.pts
    ? `Activity measured from ${fmtCount(r.gmn.n)} GMN meteors: the line is the ${_profileMeta.years[0]}–${_profileMeta.years.at(-1)} average, hatched ±1 standard deviation between the years.`
    : 'No measured activity shape: too few GMN meteors (often a daytime or outburst-only shower). The dashed line is the IAU activity period.';
  return `<figure class="sh-dc${r.pts ? '' : ' is-flat'}" data-code="${escHtml(code)}">
    <div class="sh-dc-plot"><svg aria-hidden="true"></svg></div>
    <div class="sh-dc-moon" aria-hidden="true">${moon}</div>
    <div class="sh-dc-ticks" aria-hidden="true">${ticks}</div>
    <figcaption>${escHtml(note)} The strip below is the Moon each night: the paler, the brighter.</figcaption>
  </figure>`;
}

function drawSeasonChart(panel, code) {
  const fig = panel.querySelector('.sh-dc');
  const svg = fig?.querySelector('svg');
  const r = seasonRange(code, _mdc[code] || {});
  if (!svg || !r) return;
  const w = svg.parentElement.clientWidth, h = svg.parentElement.clientHeight;
  if (!w || !h) return;
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  const x = d => (d - r.lo) / (r.hi - r.lo) * w;
  const base = h - 6;
  let body = '';
  if (r.pts) {
    const vmax = Math.max(...r.pts.map(([, m, sd]) => m + sd), 1);
    const y = v => base - (v / vmax) * (base - 10);
    const pts = r.pts.map(([d, m, sd]) => [x(d + 0.5), m, sd]);
    body = HATCH('sh-hatch-dc') + bandPaths(pts, y, 'sh-hatch-dc');
    const peak = r.pts.reduce((a, b) => (b[1] > a[1] ? b : a));
    body += `<circle class="sh-dc-dot" cx="${x(peak[0] + 0.5).toFixed(1)}" cy="${y(peak[1]).toFixed(1)}" r="4"/>`;
  } else {
    body = `<line class="sh-dc-dash" x1="${x(r.lo + 2)}" y1="${base}" x2="${x(r.hi - 2)}" y2="${base}"/>
      <circle class="sh-dc-dot" cx="${x(r.peakX + 0.5).toFixed(1)}" cy="${base}" r="4"/>`;
  }
  const today = todayDoy();
  if (today >= r.lo && today < r.hi) {
    body += `<line class="sh-total-today" x1="${x(today + 0.5).toFixed(1)}" x2="${x(today + 0.5).toFixed(1)}" y1="0" y2="${h}"/>`;
  }
  svg.innerHTML = body;
}

function detailHtml(code) {
  const info = _mdc[code] || {};
  const name = info.name || showerFullName(code);
  const count = _counts[code] || 0;
  const yearRound = YEAR_ROUND.has(code);
  const peakAt = info.sol_peak != null ? solToDate(info.sol_peak, YEAR) : null;
  const facts = [];
  if (yearRound) facts.push(['Active', 'all year']);
  else if (info.activity_begin && info.activity_end) facts.push(['Active', `${info.activity_begin} – ${info.activity_end}`]);
  if (peakAt) {
    facts.push(['Peak', `${fmtDay(peakAt)} ${YEAR}, around ${peakAt.toISOString().slice(11, 16)} UTC`]);
    const m = moonPhase(jdFromDate(peakAt));
    facts.push(['Moon at the peak', `${Math.round(m.fraction * 100)}% lit, ${moonPhaseName(m)}`]);
  } else if (info.peak) facts.push(['Peak', info.peak]);
  if (info.zhr != null) facts.push(['ZHR', `${info.zhr} per hour at the peak (IMO)`]);
  if (info.vg != null) facts.push(['Geocentric speed', `${Math.round(info.vg)} km/s`]);
  if (info.parent) facts.push(['Parent body', info.parent]);
  if (info.ra != null && info.dec != null) facts.push(['Radiant', `RA ${info.ra.toFixed(1)}°, Dec ${info.dec >= 0 ? '+' : ''}${info.dec.toFixed(1)}° (J2000)`]);
  facts.push([`ROVIMEN ${YEAR}`, count ? `${fmtCount(count)} meteors` : 'none yet']);
  const iau = info.iau_no ? `https://www.ta3.sk/IAUC22DB/MDC2022/Roje/pojedynczy_obiekt.php?kodstrumienia=${String(info.iau_no).padStart(5, '0')}` : null;
  return `<div class="sh-detail-head">
      <h2 class="sh-detail-title">${escHtml(name)} <span class="sh-code" translate="no">${escHtml(code)}</span></h2>
      <button type="button" class="sh-detail-close" data-act="close" aria-label="Close the details">&#x2715;</button>
    </div>
    <div class="sh-detail-body">
      <dl class="sh-facts">${facts.map(([k, v]) => `<div><dt>${escHtml(k)}</dt><dd>${escHtml(v)}</dd></div>`).join('')}</dl>
      ${!yearRound && peakAt ? `<p class="sh-viewing">${escHtml(viewingText(info, peakAt))}</p>` : ''}
      ${!yearRound ? seasonChart(code, info) : ''}
      <div class="sh-detail-actions">
        <a class="sh-detail-go" href="/events?shower=${encodeURIComponent(code)}">See ROVIMEN detections</a>
        ${info.wikipedia ? `<a class="sh-detail-link" href="https://en.wikipedia.org/wiki/${encodeURIComponent(info.wikipedia.replace(/ /g, '_'))}" target="_blank" rel="noopener">Wikipedia</a>` : ''}
        ${iau ? `<a class="sh-detail-link" href="${iau}" target="_blank" rel="noopener">IAU Meteor Data Center</a>` : ''}
        <a class="sh-detail-link" href="https://www.imo.net/resources/calendar/" target="_blank" rel="noopener">IMO shower calendar</a>
      </div>
    </div>`;
}

function closeDetail({ focusRow = false } = {}) {
  const panel = document.getElementById('sh-detail');
  const row = _openCode && document.querySelector(`.sh-row[data-code="${CSS.escape(_openCode)}"]`);
  panel?.remove();
  row?.setAttribute('aria-expanded', 'false');
  row?.classList.remove('is-open');
  if (focusRow) row?.focus();
  _openCode = null;
  if (location.hash) history.replaceState(null, '', location.pathname + location.search);
}

function openDetail(code, { scroll = false } = {}) {
  const row = document.querySelector(`.sh-row[data-code="${CSS.escape(code)}"]`);
  if (!row) return;
  if (_openCode) closeDetail();
  // A minor shower opened from a link is shown even under "Major".
  row.hidden = false;
  const panel = document.createElement('section');
  panel.id = 'sh-detail';
  panel.className = 'sh-detail';
  panel.setAttribute('aria-label', `${_mdc[code]?.name || code} details`);
  panel.innerHTML = detailHtml(code);
  row.after(panel);
  drawSeasonChart(panel, code);
  row.setAttribute('aria-expanded', 'true');
  row.classList.add('is-open');
  _openCode = code;
  history.replaceState(null, '', `${location.pathname}${location.search}#${encodeURIComponent(code)}`);
  panel.querySelector('[data-act="close"]').addEventListener('click', () => closeDetail({ focusRow: true }));
  if (scroll) row.scrollIntoView({ block: 'start' });
}

function _openFromHash() {
  const code = decodeURIComponent(location.hash.slice(1)).toUpperCase();
  if (code && _mdc[code]) openDetail(code, { scroll: true });
}

/* ── Zoom and pan ────────────────────────────────────────────────────────── */

const MIN_SPAN = 10;                 // days visible at the deepest zoom
const DATE_STEPS = [1, 2, 7, 14];    // date label spacing, in days
const _zoom = { start: 0, span: 365, today: 0, raf: 0 };

const _clampView = () => {
  _zoom.span = Math.min(365, Math.max(MIN_SPAN, _zoom.span));
  _zoom.start = Math.min(365 - _zoom.span, Math.max(0, _zoom.start));
};

// Slider position 0..100 <-> visible span, on a log scale (each step is the
// same relative zoom).
const _spanToSlider = span => Math.round(100 * Math.log(365 / span) / Math.log(365 / MIN_SPAN));
const _sliderToSpan = v => 365 * Math.pow(MIN_SPAN / 365, v / 100);

/** Zoom to `span` days, keeping the day under `anchorFrac` (0..1 across the
 *  track) where it is. */
function zoomTo(span, anchorFrac = 0.5) {
  const anchorDay = _zoom.start + anchorFrac * _zoom.span;
  // From the whole year, a slider zoom centres on today.
  const anchor = _zoom.span >= 365 && anchorFrac === 0.5 ? _zoom.today + 0.5 : anchorDay;
  _zoom.span = span;
  _clampView();
  _zoom.start = anchor - anchorFrac * _zoom.span;
  _applyZoom();
}

function panBy(days) {
  _zoom.start += days;
  _applyZoom();
}

function _applyZoom() {
  _clampView();
  const cal = document.getElementById('sh-cal');
  if (!cal) return;
  cal.style.setProperty('--sh-zoom', (365 / _zoom.span).toFixed(4));
  cal.style.setProperty('--sh-pan', `${(_zoom.start / 365 * 100).toFixed(4)}%`);
  const slider = document.getElementById('sh-zoom');
  if (slider && document.activeElement !== slider) slider.value = _spanToSlider(_zoom.span);
  const days = Math.round(_zoom.span);
  slider?.setAttribute('aria-valuetext', days >= 365 ? 'Whole year'
    : days > 60 ? `About ${Math.round(days / 30.4)} months` : `${days} days`);
  const zoomed = _zoom.span < 364.5;
  cal.classList.toggle('is-zoomed', zoomed);
  document.querySelectorAll('.sh-zoom-pan').forEach(b => { b.hidden = !zoomed; });
  document.getElementById('sh-zoom-prev')?.toggleAttribute('disabled', _zoom.start <= 0);
  document.getElementById('sh-zoom-next')?.toggleAttribute('disabled', _zoom.start + _zoom.span >= 365);
  if (!_zoom.raf) _zoom.raf = requestAnimationFrame(() => { _zoom.raf = 0; _drawAxis(); });
}

/** Month labels centred on the visible part of each month, and day-of-month
 *  labels once they are at least 40 px apart; today's date in gold. */
function _drawAxis() {
  const track = document.getElementById('sh-axis-track');
  const cal = document.getElementById('sh-cal');
  if (!track || !cal) return;
  const w = track.clientWidth;
  if (!w) return;
  const pxDay = w / _zoom.span;
  const x = d => (d - _zoom.start) * pxDay;
  cal.classList.toggle('show-days', pxDay >= 9);
  _drawTotal(pxDay);
  let html = '';
  for (let i = 0; i < 12; i++) {
    const a = Math.max(MONTH_START[i], _zoom.start), b = Math.min(MONTH_START[i + 1], _zoom.start + _zoom.span);
    if (b - a <= 0 || (b - a) * pxDay < 26) continue;
    const name = (b - a) * pxDay > 90 && pxDay >= 9 ? MONTH_FULL[i] : MONTH_ABBR[i];
    html += `<span class="sh-month" style="left:${x((a + b) / 2).toFixed(1)}px">${name}</span>`;
  }
  const step = DATE_STEPS.find(st => st * pxDay >= 40);
  const inView = d => d >= _zoom.start && d < _zoom.start + _zoom.span;
  if (step) {
    const todayX = inView(_zoom.today) ? x(_zoom.today + 0.5) : null;
    const labels = [];
    for (let i = 0; i < 12; i++) {
      const len = MONTH_START[i + 1] - MONTH_START[i];
      for (let day = 1; day <= len; day++) {
        const d = MONTH_START[i] + day - 1;
        const isToday = d === _zoom.today;
        // Steps count from the 1st of each month; a 7-day step skips the 29th.
        const onStep = !((day - 1) % step) && !(step > 1 && day > len - step / 2);
        if (!isToday && !onStep) continue;
        const px = x(d + 0.5);
        // Whole labels only (none cut at the edges), and none crowding today's.
        if (px < 10 || px > w - 10) continue;
        if (!isToday && todayX != null && Math.abs(px - todayX) < 26) continue;
        labels.push(`<span class="sh-date${isToday ? ' is-today' : ''}" style="left:${px.toFixed(1)}px">${day}</span>`);
      }
    }
    html += labels.join('');
  } else if (inView(_zoom.today)) {
    html += `<span class="sh-today-label" style="left:${x(_zoom.today + 0.5).toFixed(1)}px">Today</span>`;
  }
  track.innerHTML = html;
  track.style.setProperty('--today-px', `${x(_zoom.today + 0.5).toFixed(1)}px`);
}

/** Pinch (two pointers) and drag (one pointer, or a mouse when zoomed) on
 *  the calendar; Ctrl+wheel (trackpad pinch) zooms around the cursor,
 *  sideways wheel pans. A drag never follows the row's link. */
function _wireGestures() {
  const cal = document.getElementById('sh-cal');
  const pts = new Map();
  let g = null;          // the gesture in progress
  let moved = false;
  const trackBox = () => document.getElementById('sh-axis-track').getBoundingClientRect();
  const frac = clientX => { const r = trackBox(); return Math.min(1, Math.max(0, (clientX - r.left) / r.width)); };

  const begin = () => {
    const [a, b] = [...pts.values()];
    if (b) {
      g = { kind: 'pinch', dist: Math.hypot(a.x - b.x, a.y - b.y), span: _zoom.span,
            anchorDay: _zoom.start + frac((a.x + b.x) / 2) * _zoom.span };
    } else {
      g = { kind: 'pan', x: a.x, y: a.y, start: _zoom.start, axis: null };
    }
  };
  cal.addEventListener('pointerdown', e => {
    if (e.pointerType === 'mouse' && (e.button !== 0 || _zoom.span >= 365)) return;
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    moved = false;
    begin();
  });
  cal.addEventListener('pointermove', e => {
    if (!pts.has(e.pointerId) || !g) return;
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (g.kind === 'pinch' && pts.size >= 2) {
      const [a, b] = [...pts.values()];
      const dist = Math.hypot(a.x - b.x, a.y - b.y);
      if (!g.dist || !dist) return;
      _zoom.span = g.span * g.dist / dist;
      _clampView();
      _zoom.start = g.anchorDay - frac((a.x + b.x) / 2) * _zoom.span;
      moved = true;
      _applyZoom();
    } else if (g.kind === 'pan') {
      const dx = e.clientX - g.x, dy = e.clientY - g.y;
      // Decide once whether this is a sideways pan or a page scroll.
      if (!g.axis && Math.hypot(dx, dy) > 6) g.axis = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';
      if (g.axis !== 'x' || _zoom.span >= 365) return;
      try { cal.setPointerCapture(e.pointerId); } catch (_) { /* the pointer already ended */ }
      moved = true;
      _zoom.start = g.start - dx / trackBox().width * _zoom.span;
      _applyZoom();
    }
  });
  const end = e => {
    pts.delete(e.pointerId);
    g = null;
    if (pts.size) begin();     // a pinch that loses a finger carries on as a pan
  };
  ['pointerup', 'pointercancel'].forEach(t => cal.addEventListener(t, end));
  cal.addEventListener('click', e => { if (moved) { e.preventDefault(); e.stopPropagation(); moved = false; } }, true);
  // Rows are links: without this a mouse drag starts the browser's link drag
  // (which cancels the pointer) instead of panning.
  cal.addEventListener('dragstart', e => e.preventDefault());
  // A click (or Enter) on a row opens its details; a second one closes them.
  cal.addEventListener('click', e => {
    const row = e.target.closest('.sh-row');
    if (!row) return;
    e.preventDefault();
    if (_openCode === row.dataset.code) closeDetail();
    else openDetail(row.dataset.code);
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && _openCode && !e.defaultPrevented) closeDetail({ focusRow: true });
  });
  window.addEventListener('hashchange', () => {
    if (location.hash) _openFromHash(); else if (_openCode) closeDetail();
  });
  cal.addEventListener('wheel', e => {
    if (e.ctrlKey) {
      e.preventDefault();
      // A mouse-wheel notch (deltaY ~100) is one ~1.6x step; a trackpad pinch
      // sends many small deltas and zooms smoothly.
      zoomTo(_zoom.span * Math.exp(Math.max(-50, Math.min(50, e.deltaY)) * 0.01), frac(e.clientX));
    } else if (_zoom.span < 365 && (Math.abs(e.deltaX) > Math.abs(e.deltaY) || e.shiftKey)) {
      e.preventDefault();
      panBy((e.shiftKey && !e.deltaX ? e.deltaY : e.deltaX) / trackBox().width * _zoom.span);
    }
  }, { passive: false });
}

function _applyFilter() {
  const q = _searchQuery.toLowerCase();
  let n = 0;
  document.querySelectorAll('.sh-row').forEach(row => {
    let show = _scope === 'all' || row.hasAttribute('data-major');
    // A search reaches every shower, so a minor one can still be found.
    if (q) show = (row.dataset.code || '').toLowerCase().includes(q) || (row.dataset.name || '').includes(q);
    if (show && _onlyDetections && !(+row.dataset.count > 0)) show = false;
    row.hidden = !show;
    if (show) n++;
  });
  // Details whose row a filter just hid would float under the wrong row.
  if (_openCode && document.querySelector(`.sh-row[data-code="${CSS.escape(_openCode)}"]`)?.hidden) closeDetail();
  const el = document.getElementById('sh-count');
  if (el) el.textContent = `${n} shower${n !== 1 ? 's' : ''}`;
  const empty = document.getElementById('sh-empty');
  if (!n && !empty) {
    document.querySelector('.sh-rows')?.insertAdjacentHTML('afterend',
      '<p class="sh-empty" id="sh-empty">No shower matches. Clear the search or switch to All IAU.</p>');
  } else if (n && empty) empty.remove();
}

function _selectScope(scope) {
  _scope = scope === 'all' ? 'all' : 'major';
  document.querySelectorAll('.sh-scope-btn').forEach(b => {
    const on = b.dataset.scope === _scope;
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
}

// Keep the scope and search in the URL so a view can be shared or reloaded.
function _syncUrl() {
  const p = new URLSearchParams(location.search);
  p.delete('month');                       // the old month tabs
  if (_scope === 'all') p.set('scope', 'all'); else p.delete('scope');
  if (_searchQuery) p.set('q', _searchQuery); else p.delete('q');
  const qs = p.toString();
  history.replaceState(null, '', location.pathname + (qs ? `?${qs}` : '') + location.hash);
}

/* ── Init ─────────────────────────────────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', async () => {
  const searchEl = document.getElementById('sh-search');
  const initial = new URLSearchParams(location.search);
  _selectScope(initial.get('scope'));
  if (initial.get('q') && searchEl) {
    searchEl.value = initial.get('q');
    _searchQuery = searchEl.value.trim();
  }

  document.querySelectorAll('.sh-scope-btn').forEach(b => b.addEventListener('click', () => {
    _selectScope(b.dataset.scope);
    _applyFilter();
    _syncUrl();
  }));
  searchEl?.addEventListener('input', e => {
    _searchQuery = e.target.value.trim();
    _applyFilter();
    _syncUrl();
  });
  document.getElementById('sh-only-detections')?.addEventListener('change', e => {
    _onlyDetections = e.target.checked;
    _applyFilter();
  });

  const zoomEl = document.getElementById('sh-zoom');
  zoomEl?.addEventListener('input', () => zoomTo(_sliderToSpan(+zoomEl.value)));
  document.getElementById('sh-zoom-year')?.addEventListener('click', () => zoomTo(365));
  document.getElementById('sh-zoom-prev')?.addEventListener('click', () => panBy(-_zoom.span / 2));
  document.getElementById('sh-zoom-next')?.addEventListener('click', () => panBy(_zoom.span / 2));
  _wireGestures();
  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(() => _applyZoom()).observe(document.getElementById('sh-cal'));
  }

  // Counts decide which showers are "major", so wait for both; the counts
  // are optional (an empty object until the daily scan has run).
  const [mdcRes, countsRes, profRes] = await Promise.allSettled([
    fetch('/api/showers').then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); }),
    fetch('/api/shower-year-counts').then(r => r.ok ? r.json() : {}),
    // Measured activity shapes are optional: without them every row is a
    // dashed line over its MDC activity period.
    fetch('/api/shower-profiles').then(r => r.ok ? r.json() : {}),
  ]);
  if (profRes.status === 'fulfilled' && profRes.value && profRes.value.showers) {
    _profiles = profRes.value.showers;
    _profileMeta = { years: profRes.value.years, meteors: profRes.value.meteors };
    const t = profRes.value.total;
    if (t && t.series && t.series.length) {
      _total = { showers: t.showers, points: t.series.map(([sol, m, sd]) => [solDoy(sol), m, sd]).sort((a, b) => a[0] - b[0]) };
    }
    const note = document.getElementById('sh-profile-note');
    if (note) {
      note.hidden = false;
      note.querySelector('span').textContent =
        `${_profileMeta.years[0]}–${_profileMeta.years.at(-1)}, ${fmtCount(_profileMeta.meteors)} meteors`;
    }
  }
  const mdc = mdcRes.status === 'fulfilled' ? mdcRes.value : null;
  if (!mdc || !Object.keys(mdc).length) {
    const el = document.getElementById('sh-loading');
    if (el) el.textContent = 'Could not load the shower list. Reload the page to try again.';
    return;
  }
  render(mdc, countsRes.status === 'fulfilled' ? countsRes.value : {});
});
