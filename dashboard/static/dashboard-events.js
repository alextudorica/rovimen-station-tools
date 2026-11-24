import { escHtml, fetchJson, state, _toast, downloadNameFor, saveDownload, downloadFailedText, buildTrimmedShortclipUrl, parseViewParams, viewQuery, createUrlSync, clipStatus, fetchAuthStatus, reportTabView, noteUntrimmed, shortNightLabel, saveBlob, parseHHMM, deriveStackName, CLIP_PRE_S, CLIP_POST_S } from './dashboard-common.js';
import { openTimePicker, wireTimeField } from './dashboard-time-picker.js';
import { VideoModal } from './dashboard-video-modal.js';
import { MultiDetModal } from './dashboard-multi-det-modal.js';
import { gridInit, gridOnNightChange, gridCamsParam } from './dashboard-grid.js';
import { registerNavCart, navCartOpened, navCartClosed } from './dashboard-nav-carts.js';
import { withHidden, showingHidden, canHide, mountToggle, updateToggle, hideButtonHtml, hiddenNoteText, hiddenNoteHtml, hideTarget, toggleHide, markInPayload, markAll, eventListing } from './dashboard-hidden.js';
// ── Helpers ────────────────────────────────────────────────────────────────────

// A malformed time must not throw: toISOString() on an invalid Date raises
// RangeError, which used to blank the whole night's list.
function fmtDateTime(iso) {
  if (!iso) return '';
  const d = new Date(iso + (iso.endsWith('Z') ? '' : 'Z'));
  if (isNaN(d)) return String(iso);
  return d.toISOString().replace('T', ' ').slice(0, 19) + ' UTC';
}
function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso + (iso.endsWith('Z') ? '' : 'Z'));
  if (isNaN(d)) return String(iso);
  return d.toISOString().slice(11, 19) + ' UTC';
}

// A clip and its stack share a base name, so the thumbnail is derivable even
// when the detection payload carries no `stack` field (e.g. archived nights
// whose state.json only records `stacked: true`): deriveStackName.
function _stackUrlFor(hostKey, cam, date, chunk) {
  const name = chunk.stack || deriveStackName(chunk.filename);
  return name ? `/stack/${hostKey}/${cam}/${date}/${name}` : null;
}
// Full-resolution stack with the ROVIMEN/astromania branding+data strip burned
// in on demand. Cards display the small /stack thumbnail for speed, but the
// "Download stack" action must hand out the branded full-res image, so the
// download links point here (never at the un-branded thumbnail).
function _fullStackUrlFor(hostKey, cam, date, chunk) {
  const name = chunk.stack || deriveStackName(chunk.filename);
  return name ? `/fullstack/${hostKey}/${cam}/${date}/${name}` : null;
}

function _fallbackDetectionOffset(filename, meteorTimeIso) {
  if (!filename || !meteorTimeIso) return 0;
  const fnM = filename.match(/_\d{8}_(\d{6})/);
  if (!fnM) return 0;
  const tp = fnM[1];
  const chkS = +tp.slice(0,2)*3600 + +tp.slice(2,4)*60 + +tp.slice(4,6);
  const d = new Date(meteorTimeIso + (meteorTimeIso.endsWith('Z') ? '' : 'Z'));
  if (isNaN(d)) return 0;
  const detS = d.getUTCHours()*3600 + d.getUTCMinutes()*60 + d.getUTCSeconds() + d.getUTCMilliseconds()/1000;
  let diff = detS - chkS;
  if (diff < -43200) diff += 86400;
  if (diff < 0) diff = 0;
  return Math.round(diff * 100) / 100;
}

// Bumped by every renderDetections() call so a stale render stops adding
// cards to a list that has been rebuilt (evRerender, night switch).
let _detectionsRenderGen = 0;

// ── View in the URL ────────────────────────────────────────────────────────────
// /events?tab=grid&date=20260925&from=15:30&to=15:40&gfrom=15:32&gto=15:37
// keeps the night, tab, time window and Grid window across reload, Back and
// shared links; &shower=PER,ORI keeps the Sort & Filter shower selection
// (the Showers page links here with one code). from/to appear only once the user has set a window, and a
// night change keeps that window instead of re-clamping to twilight.

const EV_TABS = ['detections', 'events', 'grid', 'sortfilter'];
// cams: the Grid's camera list from the overview map's "Cameras at a point"
// (dashboard-grid.js reads it at load and owns it from then on).
const EV_URL_KEYS = ['tab', 'date', 'from', 'to', 'gfrom', 'gto', 'cams', 'shower'];
// Sort & Filter's selected showers; declared here because _evUrlQuery reads it.
let _sfSelectedShowers = new Set();
const EV_HHMM_RE = /^([01]\d|2[0-3]):[0-5]\d$/;
const _evUrlView = parseViewParams(location.search, ['tab', 'gfrom', 'gto']);
// True once the user moves the time slider or types a time; a night change
// then keeps the window rather than clamping it to twilight.
let _evUserSetRange = false;
// Slider span in slider units (twilightSlider.nightDomain); full day until
// the night's twilight is known.
let _evDomain = { lo: 0, hi: 1440 };
const _evAtStart = (v) => parseInt(v) <= _evDomain.lo;
const _evAtEnd = (v) => parseInt(v) >= _evDomain.hi;

function _evUrlQuery() {
  const val = id => document.getElementById(id)?.value || '';
  const hhmm = v => (EV_HHMM_RE.test(v) ? v : null);
  const win = _evUserSetRange
    ? { from: evSliderToFmt(val('ev-slider-start')), to: evSliderToFmt(val('ev-slider-end')) }
    : {};
  return viewQuery({
    tab: state.activeTab !== 'detections' ? state.activeTab : null,
    date: datePicker.value || null,
    ...win,
    gfrom: hhmm(val('grid-from')),
    gto: hhmm(val('grid-to')),
    cams: gridCamsParam(),
    shower: _sfSelectedShowers.size ? [..._sfSelectedShowers].join(',') : null,
  }, EV_URL_KEYS);
}
const evSyncUrl = createUrlSync(_evUrlQuery);
window.evSyncUrl = evSyncUrl;

/** Station page on `night` (RMS tab), carrying the time window if set. */
export function evStationUrl(host, night, win = _evWindow()) {
  return `/station/${encodeURIComponent(host)}/rms` +
    viewQuery({ date: night || null, ...win });
}

/** A camera's Video DB on `night`: one click from any camera name. `win`
 *  narrows it to a time window (a detection's minutes); by default it
 *  carries the window the user set here. */
export function evCameraUrl(host, cam, night, win = _evWindow()) {
  return `/station/${encodeURIComponent(host)}/videodb` +
    viewQuery({ cam, date: night || null, ...win }, ['cam', 'date', 'from', 'to', 'clip']);
}

/** The user's time window as {from, to}, or {} when they have not set one. */
function _evWindow() {
  if (!_evUserSetRange) return {};
  return {
    from: evSliderToFmt(document.getElementById('ev-slider-start').value),
    to: evSliderToFmt(document.getElementById('ev-slider-end').value),
  };
}

/** A few minutes either side of `iso` (UTC) as {from, to}: the Video DB
 *  window for one detection or event, wide enough to show its neighbours. */
export function evWindowAround(iso, minutes = 3) {
  const d = new Date(iso + (/[zZ]$/.test(iso || '') ? '' : 'Z'));
  if (!iso || isNaN(d)) return {};
  const fmt = t => `${String(t.getUTCHours()).padStart(2, '0')}:${String(t.getUTCMinutes()).padStart(2, '0')}`;
  return {
    from: fmt(new Date(d.getTime() - minutes * 60000)),
    to: fmt(new Date(d.getTime() + minutes * 60000)),
  };
}

/** Put the URL's time window on the sliders (and the Grid inputs) before the
 *  first load, so a shared link opens on the same minutes. */
function _evApplyUrlWindow() {
  const v = _evUrlView;
  if (v.from && v.to) {
    document.getElementById('ev-slider-start').value = evMinutesToSliderVal(parseHHMM(v.from), false);
    document.getElementById('ev-slider-end').value = evMinutesToSliderVal(parseHHMM(v.to), true);
    _evUserSetRange = true;
    evUpdateTimeInputs();
  }
  const gf = document.getElementById('grid-from');
  const gt = document.getElementById('grid-to');
  if (gf && EV_HHMM_RE.test(v.gfrom || '')) gf.value = v.gfrom;
  if (gt && EV_HHMM_RE.test(v.gto || '')) gt.value = v.gto;
}

// ── Scroll position across Back / reload ───────────────────────────────────────
// history.scrollRestoration is 'manual' site-wide (dashboard-common.js) and
// the list renders asynchronously, so the browser can't put the position back
// itself: Back from a station page used to land at the top of the night. Save
// it per URL on pagehide and restore it once the list is tall enough (the
// rest of the cards stream in as the bottom sentinel comes into view).
const EV_SCROLL_KEY = 'ev-scroll:';
const EV_SCROLL_WAIT_MS = 4000;
const _evScrollKey = () => EV_SCROLL_KEY + location.pathname + location.search;
window.addEventListener('pagehide', () => {
  try { sessionStorage.setItem(_evScrollKey(), String(Math.round(window.scrollY))); } catch (_) {}
});
let _evScrollTarget = (() => {
  try {
    const v = parseInt(sessionStorage.getItem(_evScrollKey()), 10);
    return Number.isFinite(v) && v > 0 ? v : null;
  } catch (_) { return null; }
})();
// The user's own scrolling wins over a pending restore.
for (const ev of ['wheel', 'touchstart', 'keydown']) {
  window.addEventListener(ev, () => { _evScrollTarget = null; }, { once: true, passive: true });
}

function _evRestoreScroll() {
  const target = _evScrollTarget;
  if (target == null) return;
  const started = Date.now();
  const step = () => {
    if (_evScrollTarget !== target) return;
    const max = document.documentElement.scrollHeight - window.innerHeight;
    window.scrollTo(0, Math.min(target, Math.max(0, max)));
    if (max >= target || Date.now() - started > EV_SCROLL_WAIT_MS) {
      _evScrollTarget = null;
      return;
    }
    setTimeout(step, 200);
  };
  requestAnimationFrame(step);
}
window.evRestoreScroll = _evRestoreScroll;   // the Grid calls it once it has rendered

// ── Night selector ─────────────────────────────────────────────────────────────

const datePicker = document.getElementById('ev-date');
datePicker.addEventListener('change', () => { evSyncUrl(); loadDetections(); gridOnNightChange(); });

async function populateNightSelector() {
  try {
    const nights = await fetchJson('/api/detections/nights');
    if (!nights.length) return;
    // "20 Oct" (the selected night gains "· 78%" once its moon is known, in
    // evDrawTwilightMarkers); the value stays YYYYMMDD.
    datePicker.innerHTML = nights.map(d => `<option value="${escHtml(d)}" data-short="${escHtml(shortNightLabel(d))}">${escHtml(shortNightLabel(d))}</option>`).join('');
    datePicker.value = nights.includes(_evUrlView.date) ? _evUrlView.date : nights[0];
    _evApplyUrlWindow();
    loadDetections();
  } catch(e) {
    console.error('Failed to load nights', e);
  }
}

// ── Time slider helpers ───────────────────────────────────────────────────

function evSliderToFmt(v) {
  const m = (parseInt(v) + 720) % 1440;
  return `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
}

// minutes-of-day \u2192 slider value (slider is noon-anchored: 0 = noon, 720 = midnight, 1440 = next noon).
function evMinutesToSliderVal(min, isEnd) {
  if (min === 720) return isEnd ? 1440 : 0;  // 12:00 \u2014 pick the matching end
  return ((min - 720) + 1440) % 1440;
}

// Sync the editable inputs to the current slider values (without clobbering an input being edited).
function evUpdateTimeInputs() {
  const sV = parseInt(document.getElementById('ev-slider-start').value);
  const eV = parseInt(document.getElementById('ev-slider-end').value);
  const sIn = document.getElementById('ev-time-start');
  const eIn = document.getElementById('ev-time-end');
  if (sIn && document.activeElement !== sIn) { sIn.value = evSliderToFmt(sV); sIn.classList.remove('invalid'); }
  if (eIn && document.activeElement !== eIn) { eIn.value = evSliderToFmt(eV); eIn.classList.remove('invalid'); }
}

function evTimeInputChange(which) {
  const inEl = document.getElementById(which === 'start' ? 'ev-time-start' : 'ev-time-end');
  const slEl = document.getElementById(which === 'start' ? 'ev-slider-start' : 'ev-slider-end');
  const min = parseHHMM(inEl.value);
  if (min === null) { inEl.classList.add('invalid'); return; }
  inEl.classList.remove('invalid');
  slEl.value = evMinutesToSliderVal(min, which === 'end');
  evSliderInput(which);  // clamps + re-renders + writes back the formatted value
}

let _evSliderTimer = null;
function evSliderInput(which) {
  const sEl = document.getElementById('ev-slider-start');
  const eEl = document.getElementById('ev-slider-end');
  if (which === 'start' && parseInt(sEl.value) > parseInt(eEl.value)) sEl.value = eEl.value;
  if (which === 'end' && parseInt(eEl.value) < parseInt(sEl.value)) eEl.value = sEl.value;
  // The window is now the user's (dragging back to the full range gives it
  // back to the twilight clamp), and it survives night changes.
  _evUserSetRange = !(_evAtStart(sEl.value) && _evAtEnd(eEl.value));
  evSyncUrl();
  evUpdateTimeInputs();
  clearTimeout(_evSliderTimer);
  _evSliderTimer = setTimeout(evRerender, 150);
}

// ── Azimuth / pointing filter ─────────────────────────────────────────────────

const EV_AZ_HALF = 50; // ±50° = 100° window
const _EV_COMPASS_AZ = {N: 0, E: 90, S: 180, W: 270};
let _camAzimuths = {};  // camCode → az_centre degrees (populated at init)
let _evAzActive = false;
let _evAzCenter = 0;
let _evStationsCache = null;

async function _evGetStations() {
  if (_evStationsCache) return _evStationsCache;
  _evStationsCache = await fetchJson('/api/stations');
  return _evStationsCache;
}

async function evLoadPlatepars() {
  // Mark compass as loading until platepars arrive
  const compassEl = document.querySelector('.ev-compass');
  if (compassEl) compassEl.classList.add('loading');
  try {
    const stations = await _evGetStations();
    await Promise.all(Object.keys(stations).map(async hk => {
      try {
        const r = await fetch(`/api/platepar/${hk}`);
        if (!r.ok) return;
        const pp = await r.json();
        for (const [cam, d] of Object.entries(pp)) {
          if (d && d.az_centre != null) _camAzimuths[cam] = d.az_centre;
        }
      } catch(e) {}
    }));
  } catch(e) {}
  // Remove loading state from compass
  if (compassEl) compassEl.classList.remove('loading');
  // Re-render now that azimuths are available (especially if user toggled a
  // compass direction while platepars were still loading)
  if (_data) {
    renderDetections(_data);
    renderEvents(_data);
  }
}

function _azCompass(deg) {
  const dirs = ['N','NE','E','SE','S','SW','W','NW'];
  return dirs[Math.round(((deg % 360) + 360) % 360 / 45) % 8];
}

function evInAzWindow(camCode) {
  if (!_evAzActive) return true;
  const az = _camAzimuths[camCode];
  if (az == null) return true; // unknown camera — show it
  return [...document.querySelectorAll('.ev-compass-slice.active')].some(s => {
    const center = _EV_COMPASS_AZ[s.dataset.dir];
    let diff = Math.abs(((az - center) + 360) % 360);
    if (diff > 180) diff = 360 - diff;
    return diff <= EV_AZ_HALF;
  });
}

function _azDiff(a, b) {
  const d = Math.abs(((a - b) + 360) % 360);
  return d > 180 ? 360 - d : d;
}

/** Circular mean of an array of azimuth degrees. */
function _azMean(azimuths) {
  const s = azimuths.reduce((a, v) => a + Math.sin(v * Math.PI / 180), 0);
  const c = azimuths.reduce((a, v) => a + Math.cos(v * Math.PI / 180), 0);
  return (Math.atan2(s, c) * 180 / Math.PI + 360) % 360;
}

/**
 * From a list of witnesses, return the largest subset where all cameras
 * are within 90° of each other (using best-anchor greedy), plus any
 * witnesses whose camera azimuth is unknown (always kept).
 * Returns null if the result spans < 2 stations.
 */
function evAzCluster(witnesses) {
  if (Object.keys(_camAzimuths).length === 0) return witnesses; // not loaded yet
  const known = witnesses.filter(w => _camAzimuths[w.cam] != null);
  const unknown = witnesses.filter(w => _camAzimuths[w.cam] == null);
  if (known.length === 0) return witnesses;

  let best = [];
  for (const anchor of known) {
    const az0 = _camAzimuths[anchor.cam];
    const cluster = known.filter(w => _azDiff(_camAzimuths[w.cam], az0) <= 90);
    if (cluster.length > best.length) best = cluster;
  }
  const result = [...best, ...unknown];
  const stations = new Set(result.map(w => w.host_key));
  return stations.size >= 2 ? result : null; // null = not a valid multi-station event
}

function evCompassClick(dir) {
  if (Object.keys(_camAzimuths).length === 0) {
    _toast('Platepars still loading — compass filter not ready yet', 'info');
    return;
  }
  const slice = document.querySelector(`.ev-compass-slice[data-dir="${dir}"]`);
  slice.setAttribute('aria-pressed', slice.classList.toggle('active') ? 'true' : 'false');
  _evAzActive = document.querySelectorAll('.ev-compass-slice.active').length > 0;
  evRerender();
}

function evRerender() {
  if (!_data) return;
  renderDetections(_data);
  renderEvents(_data);
}

/** Paint the time slider track with a 24h sky gradient + dim mask outside RMS.
 *  Renderer lives in twilight-slider.js (A-3 dedupe). */
function evDrawTwilightMarkers(tw, fleet = null) {
  const wrap = document.querySelector('.ev-slider-wrap');
  _evLastTwilight = tw;
  // The slider covers the fleet's footage window (/api/twilight/network: the
  // earliest -9° start to the latest -9° + 30 min end over every station),
  // else this station's night: daytime is not selectable. Its ends mean
  // "no limit" (evInTimeWindow).
  _evDomain = window.twilightSlider.nightDomain(fleet && fleet.capture_start_min != null ? fleet : tw);
  for (const id of ['ev-slider-start', 'ev-slider-end']) {
    const el = document.getElementById(id);
    if (el) { el.min = String(_evDomain.lo); el.max = String(_evDomain.hi); }
  }
  window.twilightSlider.drawTwilightOnWrap(wrap, tw, _evDomain);
  const pct = tw && tw.illumination != null ? Math.round(tw.illumination * 100) : null;
  for (const opt of datePicker.options) {
    if (!opt.dataset.short) continue;
    opt.textContent = opt.selected && pct != null ? `${opt.dataset.short} \u00b7 ${pct}%` : opt.dataset.short;
  }
}

/** "Whole night": drop the user's window and clamp to tonight's twilight again,
 *  like Video DB's vdbResetTimeRange. */
async function evResetTimeRange() {
  _evUserSetRange = false;
  document.getElementById('ev-slider-start').value = 0;
  document.getElementById('ev-slider-end').value = 1440;
  evUpdateTimeInputs();
  evSyncUrl();
  await evClampToTwilight(datePicker.value);
  evRerender();
}

// ── Time picker on the two time fields (dashboard-time-picker.js) ──────────
// Same behaviour as Video DB (wireTimeField): type a time straight in on
// desktop, the wheel sheet on phones.
let _evLastTwilight = null;

function evOpenTimePicker(which, extra = {}) {
  const sIn = document.getElementById('ev-time-start');
  const eIn = document.getElementById('ev-time-end');
  openTimePicker({
    ...extra,
    anchor: which === 'end' ? eIn : sIn,
    start: sIn.value, end: eIn.value, focus: which,
    skyBand: (h) => window.twilightSlider?.skyBandForHour(_evLastTwilight, h) ?? null,
    limits: _evDomain.lo > 0 || _evDomain.hi < 1440 ? _evDomain : null,
    onSet: (st, en) => {
      sIn.value = st; evTimeInputChange('start');
      eIn.value = en; evTimeInputChange('end');
    },
  });
}

(function evWireTimePickers() {
  for (const [which, id] of [['start', 'ev-time-start'], ['end', 'ev-time-end']]) {
    const el = document.getElementById(id);
    if (el) wireTimeField(el, { which, openPicker: evOpenTimePicker });
  }
})();

async function evClampToTwilight(dateYMD) {
  try {
    const stations = await _evGetStations();
    const roStation = Object.keys(stations).find(k => k.startsWith('gmnro')) || Object.keys(stations)[0];
    if (!roStation) return;
    const [r, rf] = await Promise.all([
      fetch(`/api/twilight/${roStation}/${dateYMD}`),
      fetch(`/api/twilight/network/${dateYMD}`).catch(() => null),
    ]);
    if (!r.ok) return;
    const tw = await r.json();
    const fleet = rf && rf.ok ? await rf.json() : null;
    evDrawTwilightMarkers(tw, fleet);
    // A window the user set stays put across night changes; the range inputs
    // have already pulled it inside this night's span (their new min / max).
    if (_evUserSetRange) { evUpdateTimeInputs(); return; }
    const sEl = document.getElementById('ev-slider-start');
    const eEl = document.getElementById('ev-slider-end');
    // The whole night is the whole slider (evDrawTwilightMarkers set its span).
    sEl.value = _evDomain.lo;
    eEl.value = _evDomain.hi;
    evUpdateTimeInputs();
  } catch (e) { /* keep defaults */ }
}

/** Check if an ISO meteor_time falls within the current slider window. */
function evInTimeWindow(meteorTime) {
  const sVal = parseInt(document.getElementById('ev-slider-start').value);
  const eVal = parseInt(document.getElementById('ev-slider-end').value);
  // A thumb at the end of the night span sets no limit on that side: the
  // span comes from one station's twilight, and a station further west
  // (Berlin) still has dark sky after it.
  const lowOpen = _evAtStart(sVal), highOpen = _evAtEnd(eVal);
  if (lowOpen && highOpen) return true;
  if (!meteorTime) return true;
  const d = new Date(meteorTime + (meteorTime.endsWith('Z') ? '' : 'Z'));
  if (isNaN(d)) return true;
  const utcMin = d.getUTCHours() * 60 + d.getUTCMinutes();
  const sliderVal = ((utcMin - 720) + 1440) % 1440;
  return (lowOpen || sliderVal >= sVal) && (highOpen || sliderVal <= eVal);
}

// ── Zoom slider ───────────────────────────────────────────────────────────

function evUpdateZoom(val) {
  document.documentElement.style.setProperty('--ev-card-w', val + 'px');
  document.documentElement.style.setProperty('--ev-card-h', Math.round(val * 0.745) + 'px');
  // Keep the two sliders (global filter bar + Sort&Filter toolbar) in sync
  for (const id of ['ev-zoom', 'sf-zoom']) {
    const el = document.getElementById(id);
    if (el && el.value !== String(val)) el.value = val;
  }
}

// ── Tab switching ──────────────────────────────────────────────────────────────

// state.activeTab is declared in dashboard-common.js (shared across pages);
// assigning without `let` mutates that binding.
state.activeTab = 'detections';
function switchTab(name) {
  state.activeTab = name;
  document.querySelectorAll('.ev-tab').forEach(b => {
    const on = b.id === 'tab-btn-' + name;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
    b.tabIndex = on ? 0 : -1;
  });
  document.querySelectorAll('.ev-pane').forEach(p => p.classList.remove('active'));
  document.getElementById('pane-' + name).classList.add('active');
  // The global filter bar (Night/Time/Pointing) belongs to tabs 1–2.
  // Sort & Filter and Grid have their own toolbars inside the pane.
  // Grid still uses the shared Night selector, so keep that group visible but
  // hide the Time slider + Pointing compass (the grid has its own time filter).
  // The compass filters All Detections only; Multi-camera events hides it.
  const globalBar = document.querySelector('.ev-filter-bar');
  if (globalBar) globalBar.style.display = (name === 'sortfilter') ? 'none' : '';
  const timeGroup = document.querySelector('.ev-filter-group--time');
  const pointingGroup = document.querySelector('.ev-filter-group:has(.ev-compass)');
  const isGrid = name === 'grid';
  if (timeGroup) timeGroup.style.display = isGrid ? 'none' : '';
  if (pointingGroup) pointingGroup.style.display = (isGrid || name === 'events') ? 'none' : '';
  // the card-size slider sizes detection cards; the grid has its own
  const zoomRow = document.querySelector('.ev-filter-bar .ev-zoom-row');
  if (zoomRow) zoomRow.style.display = isGrid ? 'none' : '';
  // "Show hidden" sits in each tab's own toolbar: the shared bar's toggle
  // serves tabs 1-2, the Grid toolbar has its own (the bar stays up there
  // for the Night picker), Sort & Filter hides the whole bar.
  const hiddenSlot = document.getElementById('ev-hidden-slot');
  if (hiddenSlot) hiddenSlot.hidden = isGrid || !hiddenSlot.firstElementChild;
  if (name === 'sortfilter') sfInit();
  if (name === 'grid') {
    // An empty Grid window starts from the time window the user set on the
    // other tabs rather than from "now".
    const gf = document.getElementById('grid-from');
    const gt = document.getElementById('grid-to');
    if (_evUserSetRange && gf && gt && !gf.value && !gt.value) {
      gf.value = evSliderToFmt(document.getElementById('ev-slider-start').value);
      gt.value = evSliderToFmt(document.getElementById('ev-slider-end').value);
    }
    gridInit();
  }
  evSyncUrl();
  reportTabView('/events', name);   // in-page tab: no request of its own
}

// ── Data loading ───────────────────────────────────────────────────────────────

let _data = null;
let _stationsInfo = {};

async function loadDetections() {
  const date = datePicker.value;
  if (!date) return;

  document.getElementById('detections-main').innerHTML =
    '<div class="ev-loading" role="status"><div class="ev-spinner" aria-hidden="true"></div> Loading detections…</div>';
  document.getElementById('events-main').innerHTML =
    '<div class="ev-loading" role="status"><div class="ev-spinner" aria-hidden="true"></div> Loading events…</div>';
  // Clamp time sliders to twilight before fetching, so the first render uses
  // the clamped window instead of racing the async clamp (which would show
  // detections against the wrong window until the user touched a control).
  await evClampToTwilight(date);

  try {
    const r = await fetch(withHidden(`/api/detections/${date}`));
    if (!r.ok) throw new Error(r.statusText);
    _data = await r.json();
    updateToggle(document.getElementById('ev-hidden-toggle'), _data.hidden_count || 0);
    renderDetections(_data);
    renderEvents(_data);
    _evRestoreScroll();
  } catch(e) {
    const msg = `<div class="ev-empty">Failed to load detections: ${e.message}</div>`;
    document.getElementById('detections-main').innerHTML = msg;
    document.getElementById('events-main').innerHTML = msg;
  }
}

// ── Tab 1: All Detections ──────────────────────────────────────────────────────

function renderDetections(data) {
  const el = document.getElementById('detections-main');
  const byStation = data.by_station || {};
  const dateStr = data.date || '';

  // Reset per-night card lookup — it only ever holds the currently rendered
  // night's clips, so clear before repopulating to avoid unbounded growth
  // across night switches / re-renders.
  _evCardData.clear();

  const renderGen = ++_detectionsRenderGen;

  if (Object.keys(byStation).length === 0) {
    el.innerHTML = '<div class="ev-empty">No stations available.</div>';
    return;
  }

  // Build O(1) lookups for "is this (filename, cam) part of a multi-station event?".
  const multiSet    = new Set();
  const filenameToEv = new Map();  // 'filename:cam' → event object
  for (const ev of (data.events || [])) {
    if (!ev || ev.witness_count <= 1) continue;
    for (const w of (ev.witnesses || [])) {
      const key = w.filename + ':' + w.cam;
      multiSet.add(key);
      filenameToEv.set(key, ev);
    }
  }

  // First pass: render station + camera scaffolding, and collect card HTML
  // strings into a flat array so they can be streamed into their target
  // .ev-cards containers a batch per frame instead of in one parse.
  let scaffoldHtml = '';
  const cardHtml = [];   // ordered: { containerIdx, html }
  const containerSelectors = []; // one entry per .ev-cards container
  let containerCounter = 0;

  const stationOrder = Object.entries(byStation).sort((a, b) => a[0].localeCompare(b[0]));
  const showHidden = showingHidden();
  for (const [hostKey, st] of stationOrder) {
    const cameras = st.cameras || {};
    const filteredCameras = {};
    for (const [cam, chunks] of Object.entries(cameras)) {
      if (!evInAzWindow(cam)) continue;
      const shown = chunks.filter(c => evInTimeWindow(c.meteor_time) && (showHidden || !c.hidden));
      if (shown.length) filteredCameras[cam] = shown;
    }
    const totalDetections = Object.values(filteredCameras).reduce((s, c) => s + c.length, 0);

    scaffoldHtml += `<div class="ev-station">`;
    scaffoldHtml += `<div class="ev-station-hdr">
      <a class="ev-station-name ev-link${st.online ? '' : ' offline'}" href="${escHtml(evStationUrl(hostKey, dateStr))}"
         title="Open ${escHtml(st.label)} on this night">${escHtml(st.label)}</a>
      <span class="ev-station-key">${escHtml(hostKey)}</span>
      ${totalDetections > 0 ? `<span class="ev-cam-count">${totalDetections} detection${totalDetections !== 1 ? 's' : ''}</span>` : ''}
    </div>`;

    if (!st.online) {
      scaffoldHtml += `<div class="ev-offline-msg">Station offline — showing archived detections</div>`;
    }
    for (const [camCode, chunks] of Object.entries(filteredCameras)) {
      scaffoldHtml += `<div class="ev-cam-section">`;
      scaffoldHtml += `<div class="ev-cam-label">
        <a class="ev-link" href="${escHtml(evCameraUrl(hostKey, camCode, dateStr))}"
           title="Open ${escHtml(camCode)} in the Video DB on this night">${escHtml(camCode)}</a>
        ${chunks.length > 0 ? `<span class="ev-cam-count">${chunks.length}</span>` : ''}
      </div>`;

      const containerIdx = containerCounter++;
      const sel = `ev-cards-${containerIdx}`;
      containerSelectors.push(sel);
      scaffoldHtml += `<div class="ev-cards" data-cards-id="${sel}"></div>`;

      for (const chunk of chunks) {
        // Escaped once here: the time and the stack/clip names below come
        // from station-pushed rows and land in markup and attributes.
        const mt = escHtml(fmtTime(chunk.meteor_time));
        if (chunk.pending) {
          // Detection row arrived (HTTP ingest) but the clip bytes haven't
          // reached the archive yet — render a non-clickable placeholder
          // (no ev-card-trigger class, so the delegated handler ignores it).
          // The chunk's stack webp travels ahead of the clip, so show the
          // real thumbnail under the PENDING badge whenever it has landed.
          // no_clip means the camera has no clip pipeline at all and pruned
          // that retention removed the video, so say so rather than
          // promising an upload that will never arrive.
          const { label: pLabel, badge: pBadge, title: pTitle } = clipStatus(chunk);
          const pStackUrl = _stackUrlFor(hostKey, camCode, dateStr, chunk);
          let pcard = `<div class="ev-card ev-card-pending" title="${escHtml(pTitle)}">`;
          pcard += pStackUrl
            ? `<img src="${escHtml(pStackUrl)}" loading="lazy" alt="${escHtml(camCode)} ${mt}" onerror="this.style.display='none'">`
            : `<div class="ev-card-no-img">${pLabel}</div>`;
          pcard += `<div class="ev-card-overlay">${mt}</div>`;
          pcard += `<div class="ev-card-pending-badge">${pBadge}</div>`;
          pcard += `</div>`;
          cardHtml.push({ containerIdx, html: pcard });
          continue;
        }
        const stackUrl = _stackUrlFor(hostKey, camCode, dateStr, chunk);
        const fullStackUrl = _fullStackUrlFor(hostKey, camCode, dateStr, chunk);
        const isMulti = multiSet.has(chunk.filename + ':' + camCode);
        const dlUrl = `/download/${hostKey}/${camCode}/${dateStr}/${chunk.filename}`;
        const dlFilename = chunk.filename;
        const parentEv = filenameToEv.get(chunk.filename + ':' + camCode) || null;
        _evCardData.set(`${camCode}:${dateStr}:${chunk.filename}`, {
          stackUrl: stackUrl || null,
          reencoded: !!chunk.reencoded,
          detection: chunk.rms ? {
            ...chunk.rms,
            time_utc:         chunk.meteor_time,
            camera:           camCode,
          } : null,
          parentEvent:     parentEv,
          parentEventDate: dateStr,
        });
        const cardMeta = escHtml(JSON.stringify({
          hostKey, camCode, dateStr,
          filename: chunk.filename,
          offsetS: chunk.detection_offset_s || 0,
          meteorTime: chunk.meteor_time,
          dlUrl, dlFilename,
          hidden: !!chunk.hidden,
        }));
        const hdNote = hiddenNoteText(chunk);
        let card = `<div class="ev-card ev-card-trigger${chunk.hidden ? ' hd-hidden' : ''}" data-card="${cardMeta}" role="button" tabindex="0" aria-label="Play ${escHtml(camCode)} ${mt}"${hdNote ? ` title="${escHtml(hdNote)}"` : ''}>`;
        if (stackUrl) {
          card += `<img src="${escHtml(stackUrl)}" loading="lazy" alt="${escHtml(camCode)} ${mt}" onerror="this.style.display='none'">`;
        } else {
          card += `<div class="ev-card-no-img">No image</div>`;
        }
        card += `<div class="ev-card-overlay">${mt}${chunk.hidden ? ' · hidden' : ''}</div>`;
        if (isMulti) card += `<div class="ev-card-multi">MULTI</div>`;
        card += hideButtonHtml(chunk);
        // Download stack / clip are operator-only. For an anonymous public
        // visitor the underlying endpoints are gated, so painting the hover
        // buttons would just be dead controls — omit them entirely. The card
        // itself still opens the video modal (public playback).
        if (state.AUTH_USER) {
          if (stackUrl) card += `<a class="ev-card-dl ev-card-dl-stack ev-card-dl-stack-trigger" title="Download stack" aria-label="Download stack" href="${escHtml(fullStackUrl || stackUrl)}" download>&#9633;</a>`;
          card += `<button type="button" class="ev-card-dl ev-card-dl-clip-trigger" title="Download clip" aria-label="Download clip">&#10515;</button>`;
        }
        card += `</div>`;
        cardHtml.push({ containerIdx, html: card });
      }
      scaffoldHtml += `</div>`;
    }
    scaffoldHtml += `</div>`;
  }

  if (!scaffoldHtml) {
    el.innerHTML = '<div class="ev-empty">No detections found.</div>';
    return;
  }

  el.innerHTML = scaffoldHtml;
  // Single delegated listener covers all ev-card-trigger cards, including
  // those inserted lazily via insertAdjacentHTML. All server-controlled
  // strings come from data-card (JSON, HTML-escaped at build time).
  // Named references dedupe on re-add, so re-renders don't stack listeners.
  el.addEventListener('click', _evCardClick);

  // Resolve container element handles once after scaffolding lands in the DOM.
  const containerEls = containerSelectors.map(sel =>
    el.querySelector(`[data-cards-id="${sel}"]`)
  );

  // Group consecutive cards by container so each batch produces minimal
  // insertAdjacentHTML calls.
  const BATCH = 100;
  let rendered = 0;
  function renderBatch() {
    if (rendered >= cardHtml.length) return false;
    const end = Math.min(rendered + BATCH, cardHtml.length);
    let i = rendered;
    while (i < end) {
      const containerIdx = cardHtml[i].containerIdx;
      let chunkStr = '';
      let j = i;
      while (j < end && cardHtml[j].containerIdx === containerIdx) {
        chunkStr += cardHtml[j].html;
        j++;
      }
      const target = containerEls[containerIdx];
      if (target) target.insertAdjacentHTML('beforeend', chunkStr);
      i = j;
    }
    rendered = end;
    return rendered < cardHtml.length;
  }

  // First batch renders synchronously so the user sees content immediately;
  // the rest follow a batch per frame. They used to wait for a sentinel below
  // the last station, so every station past the first 100 cards sat empty
  // under its header until the user scrolled to the bottom.
  const pump = () => {
    if (renderGen === _detectionsRenderGen && renderBatch()) requestAnimationFrame(pump);
  };
  pump();
}

// ── Tab 2: Multi-Station Events ────────────────────────────────────────────────

function renderEvents(data) {
  const el = document.getElementById('events-main');
  // Build display events: apply azimuth clustering + user filters
  const displayEvents = [];
  const showHidden = showingHidden();
  for (const ev of (data.events || [])) {
    // Detections hidden as false positives: an event whose witnesses are all
    // hidden is left out; one with some hidden stays with the others.
    const listing = eventListing(ev, showHidden);
    if (listing.total <= 1 || !listing.listed) continue;
    if (!evInTimeWindow(ev.event_time)) continue;
    let clustered = evAzCluster(listing.witnesses);
    // Hiding a witness can leave one station; the event was multi-station.
    if (!clustered && listing.witnesses.length < listing.total) clustered = listing.witnesses;
    if (!clustered) continue; // pointing-inconsistent — discard
    displayEvents.push({...ev, witnesses: clustered, witness_count: clustered.length, hidden: listing.allHidden});
  }
  const dateStr = data.date || '';

  if (displayEvents.length === 0) {
    el.innerHTML = `<div class="ev-empty">No multi-station events found for this night.<br>
      <span style="font-size:12px">Events require detections from at least 2 different stations within ${data.correlation_window_s || 1}s of each other, pointing in a similar direction.</span></div>`;
    return;
  }

  // Sort newest first
  displayEvents.sort((a, b) => b.event_time.localeCompare(a.event_time));

  let html = `<div class="ev-event-list">`;
  for (const ev of displayEvents) {
    const stationSet = new Set(ev.witnesses.map(w => w.host_key));
    const stationCount = stationSet.size;
    const camCount = ev.witness_count;
    const timeLabel = fmtDateTime(ev.event_time);

    // Compute mean pointing direction from witnesses with known azimuth
    const knownAz = ev.witnesses.map(w => _camAzimuths[w.cam]).filter(a => a != null);
    const azBadge = knownAz.length > 0
      ? `<span class="ev-badge az" title="${Math.round(_azMean(knownAz))}°">${_azCompass(_azMean(knownAz))}</span>`
      : '';

    // Attach the event object via a data attribute (JSON, HTML-escaped at
    // build time) rather than inlining it into an onclick — a delegated
    // listener reads it back. Inlining only escaped `"`, so a `'`, `\`, `<`
    // or `&` in operator-editable labels/cams could corrupt the handler.
    const evData = escHtml(JSON.stringify({ ev, dateStr }));
    html += `<div class="ev-event ev-event-trigger${ev.hidden ? ' hd-hidden' : ''}" data-event="${evData}" role="button" tabindex="0" aria-label="Play event at ${escHtml(String(timeLabel))}">`;
    html += `<div class="ev-event-hdr">
      <span class="ev-event-time">${escHtml(timeLabel)}</span>
      <div class="ev-event-badges">
        <span class="ev-badge cameras">${camCount} camera${camCount !== 1 ? 's' : ''}</span>
        <span class="ev-badge stations">${stationCount} station${stationCount !== 1 ? 's' : ''}</span>
        ${azBadge}
      </div>
    </div>`;
    html += `<div class="ev-event-stacks">`;
    for (const w of ev.witnesses) {
      const stackUrl = w.stack
        ? `/stack/${w.host_key}/${w.cam}/${dateStr}/${w.stack}`
        : null;
      html += `<div>`;
      if (stackUrl) {
        html += `<img class="ev-event-thumb" src="${escHtml(stackUrl)}" loading="lazy" alt="${escHtml(w.cam)}">`;
      }
      const pendingTag = w.pending
        ? ` <span class="ev-pending-tag" title="${escHtml(clipStatus(w).title)}">${clipStatus(w).label}</span>`
        : '';
      const camUrl = evCameraUrl(w.host_key, w.cam, dateStr,
        { ...evWindowAround(w.meteor_time || ev.event_time), clip: w.filename || null });
      html += `<div class="ev-event-thumb-info"><a class="ev-link" href="${escHtml(camUrl)}"
          title="Open ${escHtml(w.cam)} in the Video DB at this time">${escHtml(w.cam)}</a> — <a class="ev-link"
          href="${escHtml(evStationUrl(w.host_key, dateStr, {}))}" style="color:var(--text)"
          title="Open ${escHtml(w.station_label)} on this night">${escHtml(w.station_label)}</a>${pendingTag}${hiddenNoteHtml(w)}${
          w.meteor_time ? hideButtonHtml(w, `data-cam="${escHtml(w.cam)}" data-mtime="${escHtml(w.meteor_time)}"`) : ''}</div>`;
      html += `</div>`;
    }
    html += `</div></div>`;
  }
  html += `</div>`;
  el.innerHTML = html;
  // Single delegated listener covers all ev-event-trigger cards. The event
  // object comes from data-event (JSON, HTML-escaped at build time), never
  // from an inline onclick attribute.
  el.removeEventListener('click', _evEventClick);
  el.addEventListener('click', _evEventClick);
}

function _evEventClick(e) {
  if (e.target.closest('a.ev-link')) return;   // station/camera link, not the card
  const hideBtn = e.target.closest('.hd-btn');
  if (hideBtn) {
    // Hide one camera's detection; the event stays while another camera has it.
    e.stopPropagation();
    evHideClick(hideBtn.dataset.cam, hideBtn.dataset.mtime, hideBtn.dataset.hd === 'hide', 'events');
    return;
  }
  const card = e.target.closest('.ev-event-trigger');
  if (!card) return;
  const { ev, dateStr } = JSON.parse(card.dataset.event || '{}');
  if (ev) openEventModal(ev, dateStr);
}

// ── Tab 3: Sort & Filter (date-range, flat chronological, sortable) ───────────

let _sfData = null;            // last fetched flat detections list (with date)
let _sfHiddenCount = 0;        // hidden detections in that range (server count)
let _sfInited = false;

// Pre-select showers from the URL (?shower=PER, or PER,ORI as this page
// writes it) — set before sfInit() runs.
const _urlShowers = (new URLSearchParams(location.search).get('shower') || '')
  .split(',').map(c => c.trim().toUpperCase()).filter(c => /^[A-Z0-9]{2,8}$/.test(c));
const _urlShower = _urlShowers[0] || null;
_urlShowers.forEach(c => _sfSelectedShowers.add(c));

const _SF_MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

/** "Aug 9" in `year` as a UTC Date, or null. */
function _sfShowerDate(s, year) {
  const [mon, day] = String(s || '').trim().split(/\s+/);
  const mi = _SF_MONTHS.indexOf(mon);
  const d = parseInt(day, 10);
  return mi < 0 || isNaN(d) ? null : new Date(Date.UTC(year, mi, d));
}

/**
 * The 7-day range a shower link opens on: the last 7 days while the shower
 * is active, else the week around its most recent peak (so a Perseids link
 * in October shows August, not an empty week). null keeps the default.
 */
export function sfShowerRange(info, today = new Date()) {
  if (!info || !info.peak) return null;
  const t = Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate());
  const y = today.getUTCFullYear();
  const b = _sfShowerDate(info.activity_begin, y), e = _sfShowerDate(info.activity_end, y);
  if (b && e) {
    // A period across New Year (Dec 20 - Jan 12) is active on either side of it.
    const active = b <= e ? (t >= b.getTime() && t <= e.getTime()) : (t >= b.getTime() || t <= e.getTime());
    if (active) return null;
  }
  let peak = _sfShowerDate(info.peak, y);
  if (!peak) return null;
  if (peak.getTime() > t) peak = _sfShowerDate(info.peak, y - 1);
  const day = 86400000;
  const from = new Date(peak.getTime() - 3 * day);
  const to = new Date(Math.min(peak.getTime() + 3 * day, t));
  return { from: from.toISOString().slice(0, 10), to: to.toISOString().slice(0, 10) };
}

async function sfInit() {
  if (_sfInited) return;
  _sfInited = true;
  // Default range: last 7 days (today inclusive)
  const today = new Date();
  const fmt = d => d.toISOString().slice(0, 10);
  const from = new Date(today); from.setUTCDate(today.getUTCDate() - 6);
  document.getElementById('sf-from').value = fmt(from);
  document.getElementById('sf-to').value = fmt(today);
  if (_urlShower) {
    // A shower link opens on that shower's own week when it is out of season.
    try {
      const info = (await fetchJson('/api/showers'))[_urlShower];
      const r = sfShowerRange(info, today);
      if (r) {
        document.getElementById('sf-from').value = r.from;
        document.getElementById('sf-to').value = r.to;
      }
    } catch (_) { /* keep the last 7 days */ }
  }
  sfReload();
}

// The range route computes one payload per night and refuses more than this
// (routes/detections.py _RANGE_MAX_DAYS); check here first so the visitor
// gets the reason without a round-trip.
const SF_MAX_RANGE_DAYS = 92;

/** Nights in the inclusive range between two YYYY-MM-DD inputs. */
function sfRangeDays(fromIso, toIso) {
  const ms = Date.parse(toIso + 'T00:00:00Z') - Date.parse(fromIso + 'T00:00:00Z');
  return Math.round(ms / 86400000) + 1;
}

function sfDateInputToYYYYMMDD(v) {
  // <input type=date> gives YYYY-MM-DD; backend wants YYYYMMDD.
  return (v || '').replaceAll('-', '');
}

async function sfReload() {
  const fromEl = document.getElementById('sf-from');
  const toEl = document.getElementById('sf-to');
  const main = document.getElementById('sortfilter-main');
  const from = sfDateInputToYYYYMMDD(fromEl.value);
  const to = sfDateInputToYYYYMMDD(toEl.value);
  if (!from || !to) {
    main.innerHTML = '<div class="ev-empty">Pick a from and to date.</div>';
    return;
  }
  if (from > to) {
    main.innerHTML = '<div class="ev-empty">From date must be on or before To date.</div>';
    return;
  }
  if (sfRangeDays(fromEl.value, toEl.value) > SF_MAX_RANGE_DAYS) {
    main.innerHTML = `<div class="ev-empty">Pick a range of at most ${SF_MAX_RANGE_DAYS} days.</div>`;
    return;
  }
  main.innerHTML = '<div class="ev-loading" role="status"><div class="ev-spinner" aria-hidden="true"></div> Loading detections…</div>';
  try {
    const r = await fetch(withHidden(`/api/detections/range?from=${from}&to=${to}`));
    if (!r.ok) {
      // The route answers a refused range with JSON {error}; anything else
      // (a proxy error page) shows as its status.
      const body = await r.json().catch(() => null);
      throw new Error((body && body.error) || `HTTP ${r.status}`);
    }
    const payload = await r.json();
    _sfData = payload.detections || [];
    _sfHiddenCount = payload.hidden_count || 0;
    updateToggle(document.getElementById('sf-hidden-toggle'), _sfHiddenCount);
    _sfBuildShowerChips();
    sfRender();
  } catch(e) {
    _sfData = null;
    main.innerHTML = `<div class="ev-empty">Failed to load: ${escHtml(e.message)}</div>`;
  }
}

function _sfBuildShowerChips() {
  const counts = {};
  for (const d of _sfData || []) {
    const s = (d.rms && d.rms.shower) || null;
    if (s) counts[s] = (counts[s] || 0) + 1;
  }
  // A selected shower with nothing in this range stays selected, as a chip
  // with 0, so the filter never switches itself off behind the user's back.
  for (const s of _sfSelectedShowers) if (!(s in counts)) counts[s] = 0;
  const entries = Object.entries(counts).sort((a, b) => {
    // SPO last, others alphabetical
    if (a[0] === 'SPO') return 1;
    if (b[0] === 'SPO') return -1;
    return a[0].localeCompare(b[0]);
  });
  const wrap = document.getElementById('sf-shower-chips');
  if (!entries.length) {
    wrap.innerHTML = '<span class="sf-chips-empty">No RMS-detected meteors in this range.</span>';
    evSyncUrl();
    return;
  }
  // Shower codes arrive with each station's pushed detection rows (free
  // text): escape them, and let the handler read the code back from the
  // attribute instead of a JS string literal a quote could close.
  let html = '';
  for (const [s, n] of entries) {
    const active = _sfSelectedShowers.has(s) ? ' active' : '';
    html += `<button type="button" class="sf-chip${active}" data-shower="${escHtml(s)}" aria-pressed="${active ? 'true' : 'false'}" onclick="sfToggleShower(this.dataset.shower)">${escHtml(s)}<span class="sf-chip-count">${n}</span></button>`;
  }
  wrap.innerHTML = html;
  evSyncUrl();
}

function sfToggleShower(s) {
  if (_sfSelectedShowers.has(s)) _sfSelectedShowers.delete(s);
  else _sfSelectedShowers.add(s);
  // Refresh chip visuals + cards
  _sfBuildShowerChips();
  sfRender();
}

function _sfPassesShowerFilter(d) {
  if (_sfSelectedShowers.size === 0) return true;
  const s = d.rms && d.rms.shower;
  return s && _sfSelectedShowers.has(s);
}

function sfRender() {
  const main = document.getElementById('sortfilter-main');
  if (!_sfData) {
    main.innerHTML = '<div class="ev-empty">No data loaded.</div>';
    return;
  }
  // Reset the per-clip metadata map so it tracks only the cards about to be
  // rendered (renderDetections clears it too); otherwise it grows unbounded
  // across every Sort & Filter reload.
  _evCardData.clear();
  const detected = [];
  const undetected = [];
  const showHidden = showingHidden();
  for (const d of _sfData) {
    if (d.hidden && !showHidden) continue;
    if (d.rms && (d.rms.shower || d.rms.mag_apparent != null)) detected.push(d);
    else undetected.push(d);
  }
  const detectedFiltered = detected.filter(_sfPassesShowerFilter);

  const sortMode = document.getElementById('sf-sort').value;
  const cmpTimeAsc = (a, b) => (a.meteor_time || '').localeCompare(b.meteor_time || '');
  const cmpTimeDesc = (a, b) => (b.meteor_time || '').localeCompare(a.meteor_time || '');
  const magOf = d => (d.rms && d.rms.mag_apparent != null) ? d.rms.mag_apparent : null;
  const durOf = d => (d.rms && d.rms.duration_s != null) ? d.rms.duration_s : null;
  const cmpMagBright = (a, b) => {
    const ma = magOf(a), mb = magOf(b);
    if (ma == null && mb == null) return cmpTimeDesc(a, b);
    if (ma == null) return 1;
    if (mb == null) return -1;
    return ma - mb; // smaller = brighter
  };
  const cmpMagFaint = (a, b) => -cmpMagBright(a, b);
  const cmpDurDesc = (a, b) => {
    const da = durOf(a), db = durOf(b);
    if (da == null && db == null) return cmpTimeDesc(a, b);
    if (da == null) return 1;
    if (db == null) return -1;
    return db - da;
  };
  const cmpShower = (a, b) => {
    const sa = (a.rms && a.rms.shower) || 'ZZZ';
    const sb = (b.rms && b.rms.shower) || 'ZZZ';
    if (sa === sb) return cmpTimeDesc(a, b);
    return sa.localeCompare(sb);
  };
  const cmp = ({
    time_desc: cmpTimeDesc, time_asc: cmpTimeAsc,
    mag_bright: cmpMagBright, mag_faint: cmpMagFaint,
    duration_desc: cmpDurDesc, shower: cmpShower,
  })[sortMode] || cmpTimeDesc;
  detectedFiltered.sort(cmp);
  // Undetected always chronological newest-first.
  undetected.sort(cmpTimeDesc);

  let html = '';
  html += `<div class="sf-section-hdr">
    <span class="sf-section-title">RMS-detected</span>
    <span class="sf-section-sub">${detectedFiltered.length} of ${detected.length}${_sfSelectedShowers.size ? ' shown' : ''}</span>
  </div>`;
  html += '<div class="sf-cards">';
  if (!detectedFiltered.length && _sfSelectedShowers.size) {
    const which = [..._sfSelectedShowers].join(', ');
    const range = `${document.getElementById('sf-from').value} to ${document.getElementById('sf-to').value}`;
    html += `<div class="ev-empty" style="padding:0">No ${escHtml(which)} detections from ${escHtml(range)}. Change the dates above, or click the shower again to clear the filter.</div>`;
  } else if (!detectedFiltered.length) {
    html += `<div class="ev-empty" style="padding:0">No RMS detections match the current filter.</div>`;
  } else {
    for (const d of detectedFiltered) html += _sfCardHtml(d, true);
  }
  html += '</div>';

  html += `<div class="sf-section-hdr">
    <span class="sf-section-title">Locked clips without metadata</span>
    <span class="sf-section-sub">${undetected.length} clip${undetected.length === 1 ? '' : 's'} · chronological</span>
  </div>`;
  html += '<div class="sf-cards">';
  if (!undetected.length) {
    html += `<div class="ev-empty" style="padding:0">All locked clips in this range have detection metadata.</div>`;
  } else {
    for (const d of undetected) html += _sfCardHtml(d, false);
  }
  html += '</div>';

  main.innerHTML = html;
  // Single delegated listener covers all sf-card-trigger cards. openSingleVideo
  // args come from data-single (JSON, HTML-escaped at build time), never an
  // inline onclick attribute.
  main.removeEventListener('click', _sfCardClick);
  main.addEventListener('click', _sfCardClick);
}

function _sfCardClick(e) {
  // Operator-only cart / download controls handled here (args carried in
  // data-* attributes, HTML-escaped at build time) instead of inline onclick
  // handlers that interpolated unescaped station/filename data.
  const hideBtn = e.target.closest('.hd-btn');
  if (hideBtn) {
    e.stopPropagation();
    const d = JSON.parse(hideBtn.closest('.sf-card')?.dataset.single || '{}');
    evHideClick(d.cam, d.meteorTime, !d.hidden, 'events');
    return;
  }
  const cartBtn = e.target.closest('.sf-cart-toggle');
  if (cartBtn) {
    e.stopPropagation();
    compCartToggle(JSON.parse(cartBtn.dataset.cart || '{}'));
    return;
  }
  const dlBtn = e.target.closest('.ev-card-dl');
  if (dlBtn) {
    // The stack <a download> has no data-dl-url — let its native download run.
    if (dlBtn.dataset.dlUrl) {
      e.stopPropagation();
      evDownloadClip(dlBtn.dataset.dlUrl, dlBtn.dataset.dlName || '', e);
    }
    return;
  }
  if (e.target.closest('a.ev-link')) return;   // station / camera link, not the card
  const card = e.target.closest('.sf-card-trigger');
  if (!card) return;
  const d = JSON.parse(card.dataset.single || '{}');
  openSingleVideo(d.hostKey, d.cam, d.date, d.filename, d.offsetS, d.meteorTime);
}

function _sfCardHtml(d, hasMeta) {
  const isPending = !!d.pending;
  const pendingLabel = isPending ? clipStatus(d).label : null;
  const stackUrl = d.stack ? `/stack/${d.host_key}/${d.cam}/${d.date}/${d.stack}` : null;
  const fullStackUrl = d.stack ? `/fullstack/${d.host_key}/${d.cam}/${d.date}/${d.stack}` : null;
  const dt = d.meteor_time ? new Date(d.meteor_time + (d.meteor_time.endsWith('Z') ? '' : 'Z')) : null;
  const dateLabel = dt ? dt.toISOString().slice(5, 10) + ' ' + dt.toISOString().slice(11, 19) : '';
  const shower = (d.rms && d.rms.shower) || null;
  const showerCls = shower === 'SPO' ? ' spo' : '';
  const mag = (d.rms && d.rms.mag_apparent != null) ? d.rms.mag_apparent : null;
  const dur = (d.rms && d.rms.duration_s != null) ? d.rms.duration_s : null;
  const angVel = (d.rms && d.rms.angular_velocity != null) ? d.rms.angular_velocity : null;
  const magStr = mag != null ? `${mag >= 0 ? '+' : ''}${mag.toFixed(1)}m` : '';
  const durStr = dur != null ? `${dur.toFixed(1)}s` : '';
  const velStr = angVel != null ? `${angVel.toFixed(0)}°/s` : '';
  const stationCam = `${d.station_label || d.host_key} · ${d.cam}`;
  _evCardData.set(`${d.cam}:${d.date}:${d.filename}`, {
    stackUrl: stackUrl || null,
    reencoded: !!d.reencoded,
    detection: d.rms ? {
      ...d.rms,
      shower:           d.rms.shower || d.shower,
      time_utc:         d.meteor_time,
      station:          d.station_label || d.host_key,
      camera:           d.cam,
    } : null,
  });
  // openSingleVideo args ride in a data attribute (JSON, HTML-escaped at build
  // time) read back by a delegated handler — never inlined into an onclick
  // string context where a backslash/quote in any field would break it.
  const singleData = escHtml(JSON.stringify({
    hostKey: d.host_key, cam: d.cam, date: d.date,
    filename: d.filename || '',
    offsetS: d.detection_offset_s || 0,
    meteorTime: d.meteor_time,
    hidden: !!d.hidden,
  }));
  const cartKey = `${d.host_key}|${d.cam}|${d.date}|${d.filename}`;
  const inCart = compCartHas(cartKey);
  // Compilation cart + per-card download/stack are operator-only. Anonymous
  // visitors see the card (opens public playback) but none of the operator
  // controls, whose endpoints are gated server-side anyway.
  const _op = !!state.AUTH_USER;
  // Pending detections (clip still in transit to the archive) get no cart /
  // download / click affordances — there is no file to fetch yet.
  const cartBtn = (_op && !isPending) ? `<button type="button" class="sf-cart-toggle${inCart ? ' in-cart' : ''}" `
    + `data-cart="${escHtml(JSON.stringify(d))}" `
    + `title="${inCart ? 'Remove from compilation' : 'Add to compilation'}" `
    + `aria-label="${inCart ? 'Remove from compilation' : 'Add to compilation'}">${inCart ? '✓' : '+'}</button>` : '';
  const sfDlUrl = `/download/${d.host_key}/${d.cam}/${d.date}/${d.filename}`;
  const sfDlBtn = (_op && !isPending) ? (`<button type="button" class="ev-card-dl" style="top:auto;bottom:26px;opacity:0" aria-label="Download clip" `
    + `data-dl-url="${escHtml(sfDlUrl)}" data-dl-name="${escHtml(d.filename || '')}" `
    + `title="Download clip">&#10515;</button>`
    + (stackUrl ? `<a class="ev-card-dl ev-card-dl-stack" style="top:auto;bottom:26px;left:28px;opacity:0" `
    + `href="${escHtml(fullStackUrl || stackUrl)}" download title="Download stack" aria-label="Download stack">&#9633;</a>` : '')) : '';
  const trigger = isPending ? ' sf-card-pending' : ' sf-card-trigger';
  const cardTitle = isPending ? `${stationCam} — ${clipStatus(d).title}` : stationCam;
  const kbd = isPending ? '' : ` role="button" tabindex="0" aria-label="Play ${escHtml(stationCam)} ${escHtml(dateLabel)}"`;
  let html = `<div class="sf-card${trigger}${inCart ? ' in-cart' : ''}${d.hidden ? ' hd-hidden' : ''}" data-single="${singleData}" title="${escHtml(cardTitle)}"${kbd}>`;
  html += cartBtn;
  html += `<div class="sf-card-img">`;
  if (stackUrl) {
    html += `<img src="${escHtml(stackUrl)}" loading="lazy" alt="${escHtml(d.cam + ' ' + dateLabel)}">`;
  } else {
    html += `<div class="sf-card-no-img">${pendingLabel || 'no image'}</div>`;
  }
  if (shower && hasMeta) html += `<div class="sf-card-shower${showerCls}">${escHtml(shower)}</div>`;
  if (isPending) html += `<div class="ev-card-pending-badge">${clipStatus(d).badge}</div>`;
  html += `<div class="sf-card-time">${dateLabel}</div>`;
  html += sfDlBtn;
  html += hideButtonHtml(d);
  html += `</div>`;
  html += `<div class="sf-card-meta">`;
  // Station and camera names link to the station page on that night and to
  // the camera's Video DB around the meteor (signed-in visitors only: station
  // pages need a login).
  const line1 = (_op && d.host_key)
    ? `<a class="ev-link" href="${escHtml(evStationUrl(d.host_key, d.date, {}))}">${escHtml(d.station_label || d.host_key)}</a>` +
      ` · <a class="ev-link" href="${escHtml(evCameraUrl(d.host_key, d.cam, d.date, { ...evWindowAround(d.meteor_time), clip: d.filename || null }))}">${escHtml(d.cam)}</a>`
    : escHtml(d.cam);
  html += `<div class="sf-card-meta-line1" title="${escHtml(stationCam)}">${line1}</div>`;
  html += hiddenNoteHtml(d);
  if (isPending) {
    const parts = [magStr, durStr, velStr].filter(Boolean);
    html += `<div class="sf-card-meta-line2">${parts.length ? parts.join(' · ') + ' · ' : ''}pending</div>`;
  } else if (hasMeta) {
    const parts = [magStr, durStr, velStr].filter(Boolean);
    html += `<div class="sf-card-meta-line2">${parts.join(' · ') || '—'}</div>`;
  } else {
    html += `<div class="sf-card-meta-line2">${d.lock_type === 'manual' ? 'manual' : 'locked'}</div>`;
  }
  html += `</div>`;
  html += `</div>`;
  return html;
}

// ── Delegated click handler for ev-card-trigger cards ─────────────────────────
// All server-controlled strings (filename, meteorTime, dlUrl, dlFilename)
// are read from data-card (JSON, HTML-escaped at render time) so they never
// appear in an onclick attribute context. A whole-night dawn run is SSH-only
// (the station API refuses it from the network); the video modal's process
// control is clip-scoped (dashboard-common.js wireProcessClipButton).

function _evCardClick(e) {
  const hideBtn = e.target.closest('.hd-btn');
  if (hideBtn) {
    e.stopPropagation();
    const d = JSON.parse(hideBtn.closest('.ev-card-trigger')?.dataset.card || '{}');
    evHideClick(d.camCode, d.meteorTime, !d.hidden, 'events');
    return;
  }
  const dlStack = e.target.closest('.ev-card-dl-stack-trigger');
  if (dlStack) {
    e.stopPropagation();
    return; // native <a download> handles it
  }
  const dlClip = e.target.closest('.ev-card-dl-clip-trigger');
  if (dlClip) {
    e.stopPropagation();
    const card = dlClip.closest('.ev-card-trigger');
    if (!card) return;
    const d = JSON.parse(card.dataset.card || '{}');
    evDownloadClip(d.dlUrl, d.dlFilename, e);
    return;
  }
  const card = e.target.closest('.ev-card-trigger');
  if (card) {
    const d = JSON.parse(card.dataset.card || '{}');
    openSingleVideo(d.hostKey, d.camCode, d.dateStr, d.filename, d.offsetS, d.meteorTime);
  }
}

// ── Modal: single video ────────────────────────────────────────────────────────

function openSingleVideo(hostKey, cam, date, filename, offsetS, meteorTimeIso, opts = {}) {
  if (!filename) return; // pending detection — no clip to play yet
  const resolved = offsetS || _fallbackDetectionOffset(filename, meteorTimeIso);
  const extra = _evCardData.get(`${cam}:${date}:${filename}`) || {};
  const src = `/api/cached-video/${hostKey}/${cam}/${date}/${encodeURIComponent(filename)}?format=mp4`;
  const title = [cam, meteorTimeIso ? fmtDateTime(meteorTimeIso) : date].filter(Boolean).join(' · ');

  let switchToMulti = null;
  if (extra.parentEvent) {
    const ev  = extra.parentEvent;
    const dt  = extra.parentEventDate;
    const cnt = ev.witness_count || (ev.witnesses?.length ?? 0);
    switchToMulti = {
      label:   `All stations (${cnt} cameras) →`,
      onClick: () => { VideoModal.closeAll(); openEventModal(ev, dt); },
    };
  }

  _evSingleModal.open({
    src,
    title,
    detOffset:     resolved ?? null,
    trimStart:     resolved != null ? Math.max(0, resolved - CLIP_PRE_S) : 0,
    trimEnd:       resolved != null ? resolved + CLIP_POST_S : undefined,
    station:       hostKey,
    camera:        cam,
    date,
    filename,
    // Every playable detection on the Events page's own tabs is a locked
    // clip, so default to that — surfacing the (disabled) lock marker there
    // needs no caller plumbing. The Grid tab reuses this function for
    // ordinary (often unlocked) footage chunks too, so it passes the clip's
    // real flag through `opts` instead of relying on this default.
    locked:        opts.locked ?? true,
    lockType:      opts.lockType ?? 'detection',
    stack:         extra.stackUrl ? { url: extra.stackUrl } : null,
    detection:     extra.detection || null,
    // "Download clip" is the whole full-resolution clip as MP4: /download/ serves
    // the archive copy, else the station's (Grid footage and clips not
    // uploaded yet). "Download trimmed" cuts the in/out handles server-side,
    // from the archive when the clip is there, else on the station.
    download:      {
      originalUrl: `/download/${hostKey}/${cam}/${date}/${encodeURIComponent(filename)}`,
      onClick:     (btn) => _evTrimDownload(btn),
    },
    // The modal self-wires its clip-scoped Process button (lock + re-encode +
    // archive upload of this one chunk) from station/camera/date/filename.
    // Whole-night reruns stay on the station header's Process night button.
    // A clip the payload reports as re-encoded gets "Processed" instead.
    processed:     !!(opts.processed ?? extra.reencoded),
    switchToMulti,
    // Hide as a false positive: detections only (the Grid's footage mode
    // passes an unlocked chunk's lockType).
    hide: (opts.lockType ?? 'detection') === 'detection' && meteorTimeIso ? {
      target: hideTarget({ meteor_time: meteorTimeIso }, cam),
      hidden: !!(opts.hidden ?? _evIsHidden(cam, meteorTimeIso)),
      page: 'events',
      onChange: evOnHiddenChange,
    } : null,
  });
}

// ── Modal ─────────────────────────────────────────────────────────────────────

// Extra card data keyed by "cam:date:filename" — populated at card-build time
const _evCardData = new Map();

const _evModal = new MultiDetModal(
  document.getElementById('ev-modal-container')
);
const _evSingleModal = new VideoModal(
  document.getElementById('ev-single-modal-container'),
  { trim: true, nav: false }
);
if (!window._VideoModalCloseAll) window._VideoModalCloseAll = () => {
  VideoModal.closeAll();
  MultiDetModal.closeAll();
};

/** MultiDetModal tiles for an event: one per witness, the ones with a clip
 *  first. A witness whose clip is not in the archive (pending, no video,
 *  removed) has no filename, so its tile carries no `url` and shows its stack
 *  and status instead; it used to be dropped, which left a two-station event
 *  opening on one camera. */
function eventModalVideos(ev, date) {
  const tile = w => {
    const stackUrl = w.stack
      ? `/stack/${w.host_key}/${w.cam}/${date}/${w.stack}`
      : null;
    const base = { cam: w.cam, station: w.host_key, date, label: w.station_label, stackUrl,
                   hide: { target: hideTarget(w), hidden: !!w.hidden } };
    if (!w.filename) {
      const st = clipStatus(w);
      return { ...base, url: null, offset: 0, filename: null,
               unavailable: { label: st.label, title: st.title } };
    }
    return {
      ...base,
      url:         `/api/cached-video/${w.host_key}/${w.cam}/${date}/${encodeURIComponent(w.filename)}?format=mp4`,
      // Each camera's own detection can trail the event by up to the
      // correlation window; shift so every tile's t=0 is the same instant.
      offset:      (w.detection_offset_s || _fallbackDetectionOffset(w.filename, w.meteor_time))
                   + ((Date.parse(ev.event_time) - Date.parse(w.meteor_time)) / 1000 || 0),
      downloadUrl: `/shortclip/${w.host_key}/${w.cam}/${date}/${encodeURIComponent(w.filename)}`,
      filename:    w.filename,
    };
  };
  const ws = ev.witnesses || [];
  return [...ws.filter(w => w.filename), ...ws.filter(w => !w.filename)].map(tile);
}

function openEventModal(ev, date) {
  _evModal.open({ title: fmtDateTime(ev.event_time), videos: eventModalVideos(ev, date),
    windowPreSec: CLIP_PRE_S, windowPostSec: CLIP_POST_S,
    onHiddenChange: (_tile, resp) => evOnHiddenChange(resp) });
}

// ── Hidden detections (false positives a user hid; dashboard-hidden.js) ──────

/** Whether the loaded lists mark the detection at (cam, time) hidden. */
function _evIsHidden(cam, meteorTime) {
  const probe = { cam, meteor_time: meteorTime };
  const t = hideTarget(probe, cam);
  const same = it => it && hideTarget(it, cam)?.time_utc === t?.time_utc;
  for (const st of Object.values(_data?.by_station || {})) {
    const hit = (st.cameras?.[cam] || []).find(same);
    if (hit) return !!hit.hidden;
  }
  return !!(_sfData || []).find(d => d.cam === cam && same(d))?.hidden;
}

/** A hide or unhide happened (card, modal, Undo): mark every list this page
 *  holds and re-render, so the card leaves (or dims with Show hidden on). */
function evOnHiddenChange(resp) {
  if (!resp) return;
  const delta = resp.changed ? (resp.hidden ? 1 : -1) : 0;
  if (_data && markInPayload(_data, resp)) {
    _data.hidden_count = Math.max(0, (_data.hidden_count || 0) + delta);
    renderDetections(_data);
    renderEvents(_data);
  }
  if (_sfData && markAll(_sfData, resp)) {
    _sfHiddenCount = Math.max(0, _sfHiddenCount + delta);
    sfRender();
  }
  window.gridOnHiddenChange?.(resp);
  updateToggle(document.getElementById('ev-hidden-toggle'), _data?.hidden_count || 0);
  updateToggle(document.getElementById('sf-hidden-toggle'), _sfHiddenCount);
}

function evHideClick(cam, meteorTime, hide, page) {
  toggleHide(hideTarget({ meteor_time: meteorTime }, cam), hide, evOnHiddenChange, { page });
}

/** "Show hidden" in the toolbar of every tab that lists detections: the
 *  shared Night bar (All Detections, Multi-Station Events), Sort & Filter and
 *  the Grid. They share one stored choice, and every list on the page comes
 *  from the server with or without hidden detections, so a flip on any of
 *  them reloads what is loaded. Styled as the page's own toggles (grid-btn,
 *  like Other stations and Select). */
function evInitHiddenToggle() {
  const reload = () => {
    loadDetections();
    if (_sfData) sfReload();
    window.gridReload?.();
  };
  const opts = { baseClass: 'grid-btn' };
  mountToggle(document.getElementById('ev-hidden-slot'), 'ev-hidden-toggle', reload,
    { ...opts, count: _data?.hidden_count || 0 });
  mountToggle(document.getElementById('sf-hidden-slot'), 'sf-hidden-toggle', reload,
    { ...opts, count: _sfHiddenCount });
  mountToggle(document.getElementById('grid-hidden-slot'), 'grid-hidden-toggle', reload, opts);
  // The shared bar's slot is hidden on the Grid tab; the Grid's own slot
  // only in "Detections only" (footage lists every chunk).
  const evSlot = document.getElementById('ev-hidden-slot');
  if (evSlot && state.activeTab === 'grid') evSlot.hidden = true;
  window.gridSyncHiddenToggle?.();
}

// Open the shared multi-detection viewer on an ARBITRARY, operator-chosen set
// of clips (issue #896). Unlike openEventModal these need not be witnesses of a
// single coincidence event — the Grid tab hands us clips picked across nights,
// stations and detections. Reuses the one page-level MultiDetModal instance
// (same pattern as events), and drops the detection reference marker because a
// hand-built set has no single shared detection time. `videos` must already be
// in MultiDetModal shape ({ cam, station, date, url, offset, label, stackUrl,
// downloadUrl, filename }).
function openMultiVideos(videos, title) {
  if (!Array.isArray(videos) || !videos.length) return;
  _evModal.open({
    title: title || `Selected clips (${videos.length})`,
    videos,
    windowPreSec: CLIP_PRE_S, windowPostSec: CLIP_POST_S,
    showDetection: false,
    onHiddenChange: (_tile, resp) => evOnHiddenChange(resp),
  });
}

function closeModal() {
  _evModal.close();
}

// ── Compilation cart ──────────────────────────────────────────────────────────

const COMP_CART_KEY = 'rovimen_compilation_cart';
let _compCart = { clips: [] };
let _compIsAdmin = false;
let _compPollTimer = null;
let _compActiveBuildId = null;

function _compCartKey(d) {
  return `${d.host_key}|${d.cam}|${d.date}|${d.filename}`;
}

function compCartHas(key) {
  return _compCart.clips.some(c => _compCartKey(c) === key);
}

function compCartLoad() {
  try {
    const raw = localStorage.getItem(COMP_CART_KEY);
    if (raw) _compCart = JSON.parse(raw) || { clips: [] };
  } catch (e) { _compCart = { clips: [] }; }
  if (!Array.isArray(_compCart.clips)) _compCart.clips = [];
}

function compCartSave() {
  try {
    localStorage.setItem(COMP_CART_KEY, JSON.stringify(_compCart));
  } catch (e) { /* quota */ }
}

function _compClipFromDetection(d) {
  const stationLabel = d.station_label || d.host_key;
  const dt = d.meteor_time ? new Date(d.meteor_time + (d.meteor_time.endsWith('Z') ? '' : 'Z')) : null;
  const dateLabel = dt ? dt.toISOString().slice(5, 19).replace('T', ' ') : '';
  const mag = (d.rms && d.rms.mag_apparent != null) ? d.rms.mag_apparent : null;
  const shower = (d.rms && d.rms.shower) || null;
  const magStr = mag != null ? `${mag >= 0 ? '+' : ''}${mag.toFixed(1)}m` : '';
  const labelParts = [magStr, shower].filter(Boolean);
  const label = `${labelParts.join(' ')} · ${stationLabel} ${d.cam} · ${dateLabel}`.trim();
  return {
    host_key: d.host_key,
    cam: d.cam,
    date: d.date,
    filename: d.filename,
    detection_offset_s: d.detection_offset_s ?? null,
    pre: 2,
    post: 5,
    label,
    stack: d.stack || null,
  };
}

function compCartToggle(d) {
  const key = _compCartKey(d);
  const idx = _compCart.clips.findIndex(c => _compCartKey(c) === key);
  if (idx >= 0) _compCart.clips.splice(idx, 1);
  else _compCart.clips.push(_compClipFromDetection(d));
  compCartSave();
  compCartUpdateFab();
  compCartRender();
  // Cheap re-render of the SF tab so the badge state flips for that card
  if (typeof sfRender === 'function' && document.getElementById('pane-sortfilter')?.classList.contains('active')) {
    sfRender();
  }
}

function compCartRemove(idx) {
  _compCart.clips.splice(idx, 1);
  compCartSave();
  compCartUpdateFab();
  compCartRender();
  if (typeof sfRender === 'function' && document.getElementById('pane-sortfilter')?.classList.contains('active')) {
    sfRender();
  }
}

function compCartSetTrim(idx, field, val) {
  const clip = _compCart.clips[idx];
  if (!clip) return;
  const n = parseFloat(val);
  if (!isFinite(n) || n < 0) return;
  clip[field] = field === 'pre' ? Math.min(60, n) : Math.min(300, Math.max(0.5, n));
  compCartSave();
  compCartRenderFootSummary();
}

function compCartQuick(idx, pre, post) {
  const clip = _compCart.clips[idx];
  if (!clip) return;
  clip.pre = pre;
  clip.post = post;
  compCartSave();
  compCartRender();
}

function compCartClear() {
  if (!_compCart.clips.length) return;
  if (!confirm(`Clear all ${_compCart.clips.length} clip(s) from the cart?`)) return;
  _compCart.clips = [];
  compCartSave();
  compCartUpdateFab();
  compCartRender();
  if (typeof sfRender === 'function' && document.getElementById('pane-sortfilter')?.classList.contains('active')) {
    sfRender();
  }
}

function compCartUpdateFab() {
  const fab = document.getElementById('comp-fab');
  const cnt = document.getElementById('comp-fab-count');
  if (!fab || !cnt) return;
  cnt.textContent = String(_compCart.clips.length);
  fab.classList.toggle('empty', _compCart.clips.length === 0);
  fab.style.display = _compIsAdmin ? '' : 'none';
}

function compCartToggleDrawer() {
  const drawer = document.getElementById('comp-drawer');
  if (!drawer) return;
  const willOpen = !drawer.classList.contains('open');
  drawer.classList.toggle('open', willOpen);
  drawer.setAttribute('aria-hidden', willOpen ? 'false' : 'true');
  drawer.inert = !willOpen;
  const fab = document.getElementById('comp-fab');
  if (fab) fab.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
  if (willOpen) {
    compCartRender();
    navCartOpened('compilation');
    drawer.querySelector('.comp-drawer-close')?.focus();
  } else {
    if (fab && drawer.contains(document.activeElement)) fab.focus();
    navCartClosed();
  }
}

// The compilation button lives in the header's cart slot, next to the basket.
(function compCartMountButton() {
  const fab = document.getElementById('comp-fab');
  const drawer = document.getElementById('comp-drawer');
  if (!fab || !drawer) return;
  document.getElementById('nav-carts')?.appendChild(fab);
  registerNavCart('compilation', {
    label: 'Compilation', button: fab, panel: drawer,
    count: () => _compCart.clips.length,
    isOpen: () => drawer.classList.contains('open'),
    open: () => { if (!drawer.classList.contains('open')) compCartToggleDrawer(); },
    close: () => { if (drawer.classList.contains('open')) compCartToggleDrawer(); },
  });
})();

function compCartRenderFootSummary() {
  const sum = document.getElementById('comp-summary');
  if (!sum) return;
  const total = _compCart.clips.reduce((acc, c) => acc + (c.pre || 0) + (c.post || 0), 0);
  sum.textContent = `${_compCart.clips.length} clip${_compCart.clips.length !== 1 ? 's' : ''} · ~${total.toFixed(0)}s total`;
}

function compCartRender() {
  const body = document.getElementById('comp-drawer-body');
  if (!body) return;
  if (!_compCart.clips.length) {
    body.innerHTML = `<div class="comp-drawer-empty">
      Pick clips with the <strong>+</strong> button on each card in the Sort &amp; Filter view.<br><br>
      Each row gets its own pre / post seconds and the cart persists across reloads.
    </div>`;
    compCartRenderFootSummary();
    return;
  }
  const rows = _compCart.clips.map((c, i) => {
    const stackUrl = c.stack ? `/stack/${c.host_key}/${c.cam}/${c.date}/${encodeURIComponent(c.stack)}` : '';
    const thumb = stackUrl
      ? `<div class="comp-row-thumb" style="background-image:url('${escHtml(stackUrl)}')"></div>`
      : `<div class="comp-row-thumb"></div>`;
    return `<div class="comp-row" draggable="true" data-idx="${i}"
       ondragstart="compCartDragStart(event,${i})"
       ondragover="compCartDragOver(event)"
       ondrop="compCartDrop(event,${i})"
       ondragend="compCartDragEnd(event)">
      ${thumb}
      <div class="comp-row-body">
        <div class="comp-row-meta">${escHtml(c.label || `${c.cam} · ${c.date}`)}</div>
        <div class="comp-row-sub">${escHtml(c.filename)}</div>
        <div class="comp-row-trim">
          <span>pre</span>
          <input type="number" min="0" max="60" step="0.5" value="${c.pre}" onchange="compCartSetTrim(${i},'pre',this.value)">
          <span>post</span>
          <input type="number" min="0.5" max="300" step="0.5" value="${c.post}" onchange="compCartSetTrim(${i},'post',this.value)">
        </div>
        <div class="comp-quick">
          <button type="button" onclick="compCartQuick(${i},2,5)">Std</button>
          <button type="button" onclick="compCartQuick(${i},5,30)">+train</button>
          <button type="button" onclick="compCartQuick(${i},3,15)">+fireball</button>
        </div>
        <button type="button" class="comp-row-trim-toggle" onclick="compRowToggleTrim(${i},this)">&#9662; Trim visually</button>
      </div>
      <button type="button" class="comp-row-rm" onclick="compCartRemove(${i})" title="Remove" aria-label="Remove from compilation">×</button>
    </div>`;
  }).join('');
  body.innerHTML = rows;
  compCartRenderFootSummary();
}

/* ─── Visual trim: per-row collapsible panel ─────────────────────────────────
   Loads the source video for the clicked clip and shows a live preview plus
   two range thumbs (start / end) anchored to the meteor's detection offset.
   Dragging either thumb seeks the player; values feed back into the cart's
   pre/post fields so the build pipeline sees them. */
function compRowToggleTrim(idx, btn) {
  const row = btn.closest('.comp-row');
  if (!row) return;
  const existing = row.querySelector('.comp-row-trim-visual');
  if (existing) {
    existing.remove();
    btn.innerHTML = '&#9662; Trim visually';
    return;
  }
  btn.innerHTML = '&#9652; Hide trim';
  const c = _compCart.clips[idx];
  const apiHost = c.host_key;
  const url = `/video/${encodeURIComponent(apiHost)}/${encodeURIComponent(c.cam)}/${encodeURIComponent(c.date)}/${encodeURIComponent(c.filename)}?format=mp4`;
  // Meteor center inside the chunk. Falls back to 10 s (typical 20 s chunk
  // midpoint) if the cart row was added before detection_offset_s was
  // captured, so the panel still works.
  const center = (c.detection_offset_s != null) ? c.detection_offset_s : 10.0;
  const initStart = Math.max(0, center - (c.pre ?? 2));
  const initEnd   = center + (c.post ?? 5);

  const panel = document.createElement('div');
  panel.className = 'comp-row-trim-visual';
  panel.innerHTML = `
    <video class="comp-trim-video" preload="metadata" muted playsinline></video>
    <div class="comp-trim-bar">
      <span>Start</span>
      <input type="range" class="comp-trim-start" min="0" max="20" step="0.1" value="${initStart.toFixed(1)}">
      <span class="comp-trim-val comp-trim-start-val">${initStart.toFixed(1)}s</span>
    </div>
    <div class="comp-trim-bar">
      <span>End</span>
      <input type="range" class="comp-trim-end" min="0" max="20" step="0.1" value="${initEnd.toFixed(1)}">
      <span class="comp-trim-val comp-trim-end-val">${initEnd.toFixed(1)}s</span>
    </div>
    <div class="comp-trim-meta">Meteor at <b>${center.toFixed(2)}s</b> in clip &middot; ffmpeg will keep <b>[start &rarr; end]</b> &middot; current pre/post: <b class="comp-trim-pp">${(c.pre ?? 0).toFixed(2)}s / ${(c.post ?? 0).toFixed(2)}s</b></div>
  `;
  row.appendChild(panel);

  const video    = panel.querySelector('video');
  const startEl  = panel.querySelector('.comp-trim-start');
  const endEl    = panel.querySelector('.comp-trim-end');
  const startVal = panel.querySelector('.comp-trim-start-val');
  const endVal   = panel.querySelector('.comp-trim-end-val');
  const ppMeta   = panel.querySelector('.comp-trim-pp');
  video.src = url;
  // controls visible only after metadata loads — avoids the "broken video"
  // first-frame flash on slow proxies.
  video.controls = false;
  video.addEventListener('loadedmetadata', () => {
    video.controls = true;
    const dur = Number.isFinite(video.duration) ? video.duration : 20;
    startEl.max = dur.toFixed(1);
    endEl.max   = dur.toFixed(1);
    if (parseFloat(endEl.value) > dur) {
      endEl.value = dur.toFixed(1);
      endVal.textContent = `${dur.toFixed(1)}s`;
    }
    video.currentTime = parseFloat(startEl.value);
  });

  const apply = () => {
    let s = parseFloat(startEl.value);
    let e = parseFloat(endEl.value);
    if (e <= s + 0.5) e = s + 0.5;
    if (s >= e - 0.5) s = e - 0.5;
    startEl.value = s.toFixed(1); endEl.value = e.toFixed(1);
    startVal.textContent = `${s.toFixed(1)}s`;
    endVal.textContent   = `${e.toFixed(1)}s`;
    const newPre  = Math.max(0,   +(center - s).toFixed(2));
    const newPost = Math.max(0.5, +(e - center).toFixed(2));
    _compCart.clips[idx].pre  = newPre;
    _compCart.clips[idx].post = newPost;
    compCartSave();
    // Sync the existing pre/post number inputs in the row header.
    const trimInputs = row.querySelectorAll('.comp-row-trim input[type="number"]');
    if (trimInputs[0]) trimInputs[0].value = newPre;
    if (trimInputs[1]) trimInputs[1].value = newPost;
    if (ppMeta) ppMeta.textContent = `${newPre.toFixed(2)}s / ${newPost.toFixed(2)}s`;
    compCartRenderFootSummary();
  };
  startEl.addEventListener('input', () => {
    apply();
    if (Number.isFinite(video.duration)) video.currentTime = parseFloat(startEl.value);
  });
  endEl.addEventListener('input', () => {
    apply();
    // Seek a hair before the end-cut so the user sees the last frame
    // they'll keep, not the one they're about to drop.
    if (Number.isFinite(video.duration)) {
      const target = Math.max(parseFloat(startEl.value), parseFloat(endEl.value) - 0.5);
      video.currentTime = target;
    }
  });
}

// ── Drag-to-reorder (HTML5 native) ────────────────────────────────────────────
let _compDragIdx = null;
function compCartDragStart(ev, idx) {
  _compDragIdx = idx;
  ev.dataTransfer.effectAllowed = 'move';
  ev.currentTarget.classList.add('dragging');
}
function compCartDragOver(ev) { ev.preventDefault(); ev.dataTransfer.dropEffect = 'move'; }
function compCartDrop(ev, targetIdx) {
  ev.preventDefault();
  if (_compDragIdx === null || _compDragIdx === targetIdx) return;
  const moved = _compCart.clips.splice(_compDragIdx, 1)[0];
  _compCart.clips.splice(targetIdx, 0, moved);
  _compDragIdx = null;
  compCartSave();
  compCartRender();
}
function compCartDragEnd(ev) {
  ev.currentTarget.classList.remove('dragging');
  _compDragIdx = null;
}

// ── Build pipeline ────────────────────────────────────────────────────────────
async function compCartBuild() {
  if (!_compCart.clips.length) return;
  const titleEl = document.getElementById('comp-title');
  const scheduleEl = document.getElementById('comp-schedule');
  const ytEnabledEl = document.getElementById('comp-yt-upload');
  const ytPrivacyEl = document.getElementById('comp-yt-privacy');
  const title = (titleEl?.value || '').trim() || `ROVIMEN Compilation · ${new Date().toISOString().slice(0,10)}`;
  const scheduledFor = scheduleEl?.value ? new Date(scheduleEl.value).toISOString() : null;
  const youtube = ytEnabledEl?.checked
    ? { upload_after_build: true, privacy: ytPrivacyEl?.value || 'unlisted' }
    : { upload_after_build: false };

  // Step 1 — create the manifest
  const buildBtn = document.getElementById('comp-build-btn');
  buildBtn.disabled = true;
  buildBtn.textContent = scheduledFor ? 'Saving…' : 'Submitting…';
  let manifestId = null;
  try {
    const resp = await fetch('/api/compilation', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title,
        scheduled_for: scheduledFor,
        clips: _compCart.clips.map(c => ({
          host_key: c.host_key, cam: c.cam, date: c.date, filename: c.filename,
          detection_offset_s: c.detection_offset_s ?? null,
          pre: c.pre, post: c.post, label: c.label,
        })),
        youtube,
      }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const created = await resp.json();
    manifestId = created.id;
  } catch (e) {
    alert(`Failed to create compilation: ${e.message}`);
    buildBtn.disabled = false;
    buildBtn.textContent = 'Build now';
    return;
  }

  // If scheduled, we're done: the dashboard's minute tick starts the build.
  if (scheduledFor) {
    buildBtn.disabled = false;
    buildBtn.textContent = 'Build now';
    alert(`Scheduled: "${title}" will build at ${scheduleEl.value}`);
    return;
  }

  // Step 2 — kick off immediate build, then poll status
  try {
    const resp = await fetch(`/api/compilation/${manifestId}/build`, { method: 'POST' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  } catch (e) {
    alert(`Build kickoff failed: ${e.message}`);
    buildBtn.disabled = false;
    buildBtn.textContent = 'Build now';
    return;
  }

  _compActiveBuildId = manifestId;
  buildBtn.textContent = 'Building…';
  compCartShowProgress({ status: 'building', progress: { phase: 'queued', done: 0, total: _compCart.clips.length }});
  compCartStartPoll(manifestId);
}

function compCartShowProgress(state) {
  const el = document.getElementById('comp-progress');
  if (!el) return;
  if (!state) { el.style.display = 'none'; return; }
  el.style.display = '';
  const p = state.progress || { phase: '?', done: 0, total: 0 };
  const pct = p.total ? Math.round(100 * p.done / p.total) : 0;
  let body;
  if (state.status === 'error') {
    body = `<div class="comp-progress-error"><strong>Build failed.</strong><br>${
      escHtml(state.error || 'Unknown error')
    }</div>`;
  } else if (state.status === 'unknown') {
    body = '<div>Build status unknown: reload the page to check it.</div>';
  } else if (state.status === 'done') {
    const dl = `<a href="/api/compilation/${_compActiveBuildId}/download" target="_blank" style="color:var(--blue)">Download MP4</a>`;
    const yt = state.youtube_url
      ? ` · <a href="${state.youtube_url}" target="_blank" style="color:var(--blue)">YouTube</a>`
      : '';
    body = `<strong>Done.</strong> ${dl}${yt}`;
  } else if (state.status === 'uploading') {
    body = `<div>Uploading to YouTube — ${p.done}%</div>
            <div class="comp-progress-bar"><div style="width:${p.done}%"></div></div>`;
  } else {
    body = `<div>${p.phase} — ${p.done}/${p.total} (${pct}%)</div>
            <div class="comp-progress-bar"><div style="width:${pct}%"></div></div>`;
  }
  el.innerHTML = body;
}

let _compYtStatus = null;  // { ready, configured, libs_ok, reason } from /api/youtube/status

function compCartYtToggle() {
  const cb = document.getElementById('comp-yt-upload');
  const sel = document.getElementById('comp-yt-privacy');
  if (sel) sel.disabled = !cb?.checked;
}

async function compCartLoadYtStatus() {
  try {
    const r = await fetch('/api/youtube/status');
    if (!r.ok) return;
    _compYtStatus = await r.json();
  } catch (e) { return; }
  const cb = document.getElementById('comp-yt-upload');
  const lbl = document.getElementById('comp-yt-label');
  const warn = document.getElementById('comp-yt-warn');
  const sel = document.getElementById('comp-yt-privacy');
  if (!cb || !lbl || !warn) return;
  if (_compYtStatus.ready) {
    cb.disabled = false;
    lbl.style.opacity = '1';
    warn.style.display = 'none';
  } else {
    cb.disabled = true;
    cb.checked = false;
    if (sel) sel.disabled = true;
    lbl.style.opacity = '0.5';
    warn.style.display = '';
    warn.textContent = _compYtStatus.reason || 'YouTube not configured';
  }
}

// The build-status poll gives up after this many failed polls in a row, or
// after this long: a dashboard restart mid-build leaves the manifest on
// "building" for good, and the poll used to run every 1.5 s forever.
const COMP_POLL_MS = 1500;
const COMP_POLL_MAX_ERRORS = 3;
const COMP_POLL_MAX_MS = 30 * 60 * 1000;
let _compPollErrors = 0;
let _compPollStarted = 0;

function compCartStartPoll(manifestId) {
  if (_compPollTimer) clearInterval(_compPollTimer);
  _compPollErrors = 0;
  _compPollStarted = Date.now();
  _compPollTimer = setInterval(() => compCartPoll(manifestId), COMP_POLL_MS);
}

function compCartStopPoll() {
  clearInterval(_compPollTimer);
  _compPollTimer = null;
  const buildBtn = document.getElementById('comp-build-btn');
  if (buildBtn) { buildBtn.disabled = false; buildBtn.textContent = 'Build now'; }
}

async function compCartPoll(manifestId) {
  if (Date.now() - _compPollStarted > COMP_POLL_MAX_MS) {
    compCartStopPoll();
    compCartShowProgress({ status: 'unknown' });
    return;
  }
  if (document.hidden) return;
  try {
    const resp = await fetch(`/api/compilation/${manifestId}/status`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const state = await resp.json();
    _compPollErrors = 0;
    compCartShowProgress(state);
    if (state.status === 'done' || state.status === 'error') compCartStopPoll();
  } catch (e) {
    _compPollErrors += 1;
    if (_compPollErrors >= COMP_POLL_MAX_ERRORS && _compPollTimer) {
      compCartStopPoll();
      compCartShowProgress({ status: 'unknown' });
    }
  }
}

async function compCartShowList() {
  // Reject non-https YouTube URLs to block javascript: and data: hrefs.
  function safeYtUrl(url) {
    if (!url) return null;
    try {
      const u = new URL(url);
      return u.protocol === 'https:' ? url : null;
    } catch { return null; }
  }
  try {
    const resp = await fetch('/api/compilation');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const items = await resp.json();
    const w = window.open('', '_blank', 'width=720,height=600');
    const rows = items.map(m => {
      const safeYt = safeYtUrl(m.youtube_url);
      const ytLink = safeYt
        ? `<a href="${escHtml(safeYt)}" target="_blank" rel="noopener noreferrer">YouTube</a>` : '';
      // Compilation IDs are dashboard-internal (UUID/integer) — still escaped for defence-in-depth.
      const safeId = escHtml(m.id);
      const dlLink = m.output_path
        ? `<a href="/api/compilation/${safeId}/download" target="_blank">Download MP4</a>` : '';
      const err = m.error
        ? `<div style="color:#e74c3c;font-size:11px;margin-top:4px">${escHtml(m.error)}</div>` : '';
      const sched = m.scheduled_for ? ` · scheduled ${escHtml(m.scheduled_for)}` : '';
      return `<div class="comp-list-item">
        <div class="comp-list-item-title">${escHtml(m.title || m.id)}</div>
        <div style="font-size:10px;color:#888">${safeId} · ${(m.clips || []).length} clips · ${escHtml(m.created_at?.slice(0,16) || '')}${sched}</div>
        <span class="comp-status-badge comp-status-${escHtml(m.status)}">${escHtml(m.status)}</span>
        <div class="comp-list-item-actions">
          ${dlLink}
          ${ytLink}
        </div>${err}
      </div>`;
    }).join('') || '<p style="text-align:center;color:#888">No compilations yet.</p>';
    w.document.write(`<html><head><title>Past compilations</title>
      <style>body{background:#0d1117;color:#c9d1d9;font-family:system-ui;padding:14px}
      a{color:#58a6ff} .comp-list-item{padding:10px;background:#161b22;border:1px solid #30363d;border-radius:6px;margin-bottom:8px}
      .comp-list-item-title{font-weight:600;margin-bottom:4px}
      .comp-status-badge{display:inline-block;padding:1px 6px;border-radius:3px;font-size:10px;margin-top:4px}
      .comp-status-draft{background:#444}.comp-status-scheduled{background:#7c3aed;color:#fff}
      .comp-status-building,.comp-status-uploading{background:#f59e0b;color:#000}
      .comp-status-done{background:#10b981;color:#000}.comp-status-error{background:#e74c3c;color:#fff}
      .comp-list-item-actions{margin-top:6px}
      .comp-list-item-actions a{font-size:11px;padding:2px 6px;background:#161b22;border:1px solid #30363d;border-radius:3px;text-decoration:none;color:#c9d1d9;margin-right:6px}
      </style></head><body><h2>Past compilations</h2>${rows}</body></html>`);
    w.document.close();
  } catch (e) {
    alert(`Failed to load list: ${e.message}`);
  }
}

// ── Clip download (fetch-first, format=mp4) ───────────────────────────────────

async function evDownloadClip(url, filename, ev) {
  const btn = ev ? ev.currentTarget : null;
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  try {
    const resp = await saveDownload(url, filename);
    if (resp.status !== 200) _toast(downloadFailedText(resp.status), 'error');
  } catch(e) {
    _toast('Download failed', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '⬇'; }
  }
}

// Trim-aware download for the events single-clip modal. Reads the modal's
// in/out handles at click time and trims the archived clip server-side via
// /api/archive/shortclip, then downloads the returned blob under the server's
// Content-Disposition name (blob URLs always honour the download attribute).
// Mirrors dashboard-archive.js _arcTrimDownload — events clips are archived by
// definition (every playable detection is a locked, uploaded clip).
async function _evTrimDownload(btn) {
  const ts = _evSingleModal.getTrimState();
  if (!ts) return;
  const { station, camera, date, filename } = ts;
  if (!camera || !date || !filename) return;
  const dlBtn = btn || _evSingleModal._el?.querySelector('.vm-download-btn');
  const origHtml = dlBtn ? dlBtn.innerHTML : null;
  if (dlBtn) { dlBtn.disabled = true; dlBtn.textContent = 'Preparing…'; }
  try {
    const enc = encodeURIComponent(filename);
    let resp = await fetch(buildTrimmedShortclipUrl(
      `/api/archive/shortclip/${camera}/${date}/${enc}`, ts).url);
    // Not in the archive (Grid footage, a clip still uploading): cut it on
    // the station instead.
    if (resp.status === 404 && station) {
      resp = await fetch(buildTrimmedShortclipUrl(
        `/shortclip/${station}/${camera}/${date}/${enc}`, ts).url);
    }
    if (!resp.ok) {
      _toast(resp.status === 404 ? 'Clip no longer available' : `Download failed (${resp.status})`, 'error');
      return;
    }
    noteUntrimmed(resp);
    const cd = resp.headers.get('content-disposition') || '';
    const m  = cd.match(/filename="?([^";\n]+)"?/);
    saveBlob(await resp.blob(), m ? m[1] : downloadNameFor(filename, resp.headers.get('Content-Type')));
  } catch (e) {
    _toast('Download failed', 'error');
  } finally {
    if (dlBtn) { dlBtn.disabled = false; dlBtn.innerHTML = origHtml; }
  }
}

// ── Auth + init ────────────────────────────────────────────────────────────────

async function _evInit() {
  // Wait for the session BEFORE any gated fetchJson fires so an anonymous
  // visitor on this public page isn't bounced to /login when the
  // (login-gated) detection feed 401s — fetchJson keys its login redirect
  // off state.AUTH_USER, which fetchAuthStatus writes.
  const auth = await fetchAuthStatus();
  if (auth && auth.admin) _compIsAdmin = true;
  evInitHiddenToggle();
  compCartLoad();
  compCartUpdateFab();
  if (_compIsAdmin) compCartLoadYtStatus();
  // Kick the data loads only after auth state is known.
  reportTabView('/events', state.activeTab);
  const nightsReady = populateNightSelector();
  evLoadPlatepars();
  if (_urlShower) {
    switchTab('sortfilter');
  } else if (EV_TABS.includes(_evUrlView.tab) && _evUrlView.tab !== 'detections') {
    // The Grid reads the night picker, so open the tab once nights are in.
    nightsReady.then(() => switchTab(_evUrlView.tab));
  }
}
_evInit();

// Tablist keyboard model: Left/Right/Home/End move between tabs and
// activate them (automatic activation; every pane is cheap to show).
document.querySelector('.ev-tabs')?.addEventListener('keydown', e => {
  const tabs = [...document.querySelectorAll('.ev-tabs .ev-tab')]
    .filter(t => t.offsetParent !== null);
  const i = tabs.indexOf(document.activeElement);
  if (i < 0) return;
  let j = null;
  if (e.key === 'ArrowRight') j = (i + 1) % tabs.length;
  else if (e.key === 'ArrowLeft') j = (i - 1 + tabs.length) % tabs.length;
  else if (e.key === 'Home') j = 0;
  else if (e.key === 'End') j = tabs.length - 1;
  if (j === null) return;
  e.preventDefault();
  tabs[j].focus();
  switchTab(tabs[j].id.replace('tab-btn-', ''));
});

// Enter / Space on a focused clip card or event row opens it, like a click.
document.addEventListener('keydown', e => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const t = e.target;
  if (!(t instanceof window.Element) || t.getAttribute('role') !== 'button') return;
  if (!t.matches('.ev-card-trigger, .ev-event-trigger, .sf-card-trigger')) return;
  e.preventDefault();
  t.click();
});

// Escape closes the compilation drawer.
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape' || e.defaultPrevented) return;
  if (document.querySelector('.vm-backdrop.open, .mdm-backdrop.open')) return;
  const drawer = document.getElementById('comp-drawer');
  if (drawer && drawer.classList.contains('open')) compCartToggleDrawer();
});

// Expose to global scope for inline onclick handlers
window.switchTab = switchTab;
window.closeModal = closeModal;
window.evCompassClick = evCompassClick;
window.compCartBuild = compCartBuild;
window.compCartClear = compCartClear;
window.compCartShowList = compCartShowList;
window.compCartToggleDrawer = compCartToggleDrawer;
window.compCartQuick = compCartQuick;
window.compCartRemove = compCartRemove;
window.compCartSetTrim = compCartSetTrim;
window.compCartYtToggle = compCartYtToggle;
window.openEventModal = openEventModal;
window.openSingleVideo = openSingleVideo;
// The Grid's thumbs hide through this too (dashboard-grid.js), so every list
// on the page follows one hide.
window.evOnHiddenChange = evOnHiddenChange;
window.openMultiVideos = openMultiVideos;
window.sfToggleShower = sfToggleShower;
window.compRowToggleTrim = compRowToggleTrim;
window.evSliderInput = evSliderInput;
window.evTimeInputChange = evTimeInputChange;
window.evResetTimeRange = evResetTimeRange;
window.evUpdateZoom = evUpdateZoom;
window.sfReload = sfReload;
window.sfRender = sfRender;
window.compCartToggle = compCartToggle;
window.compCartDragStart = compCartDragStart;
window.compCartDragOver = compCartDragOver;
window.compCartDrop = compCartDrop;
window.compCartDragEnd = compCartDragEnd;
window.evDownloadClip = evDownloadClip;
