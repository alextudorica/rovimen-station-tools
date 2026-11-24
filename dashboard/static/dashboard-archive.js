import { fmtBytes, parseFn, _modalOpen, _modalClose, _closeAllOpenModals, escHtml, state, buildTrimmedShortclipUrl, nearestNight, utcSecondKey, saveBlob, nightTitle } from './dashboard-common.js';
import { VideoModal } from './dashboard-video-modal.js';
import { withHidden, showingHidden, toggleHtml, wireToggle, updateToggle, hideButtonHtml, hiddenNoteHtml, hideTarget, toggleHide, applyHidden } from './dashboard-hidden.js';
/*
 * dashboard-archive.js
 * ────────────────────
 * Archive tab — permanent storage box browsing (recordings + nightly timelapse), archive video modal. Loaded by dashboard.html before dashboard-station.js.
 *
 * Depends on dashboard-common.js (escHtml, fmtBytes, fmtDate, parseFn, state.STATIONS_META,
 * state.VDB_CAMERAS, state.statusData, state.vitalsData, state.tlData, state.activeStation, state.activeTab, state.IS_ADMIN,
 * state.AUTH_USER, state.USER_ROLE, state.USER_STATIONS, _modalOpen, _modalClose) and lexical bindings
 * declared at the top level of dashboard-station.js (classic non-module scripts share
 * the global lexical environment, so top-level `let`/`const` are visible across bundles).
 */

/* ─────────────────────────────────────────
   Archive tab
───────────────────────────────────────── */
let archiveState = { station: null, selectedCamera: null, data: {} }; // data: {camera: [{date, meteors, timelapse}]}
const _arcCardData = new Map(); // keyed by filename → { stackUrl, cam, date, meteor_time, hidden }

/* Detections hidden as false positives (dashboard-hidden.js): the archive
   lists them only with "Show hidden"; a hide on this page marks the clip in
   place and the lists below filter it out (so Undo brings it back). */
function _arcListed(night) {
  const show = showingHidden();
  return (night.meteors || []).filter(m => show || !m.hidden);
}

/** The camera's nights that list something: a clip or the timelapse. */
function _arcNights(cam) {
  return (archiveState.data[cam] || []).filter(n => _arcListed(n).length || n.timelapse);
}

/** "1,234 meteor clips over 56 nights": the clips the tab lists now. */
function _arcTitle(cam) {
  const nights = _arcNights(cam);
  if (!nights.length) return `${cam} has nothing archived yet`;
  const clips = nights.reduce((s, n) => s + _arcListed(n).length, 0);
  return `${clips.toLocaleString('en-GB')} meteor clip${clips !== 1 ? 's' : ''} over ${nights.length} night${nights.length !== 1 ? 's' : ''}`;
}

/** Hidden clips of the camera's archive (the server's per-night count). */
function _arcHiddenCount(cam) {
  return (archiveState.data[cam] || []).reduce((s, n) => s + (n.hidden_count || 0), 0);
}

const NIGHTS_PER_PAGE = 7;

let _archiveImgObserver = null;
let _archiveSentinelObserver = null;
let _archiveRenderedCount = 0;

function _archiveCamerasFor(host) {
  // Prefer the live station settings when available (lets newly-added RMS
  // cams appear without a dashboard-config edit), but fall back to the
  // dashboard's state.STATIONS_META — which is the source of truth for which
  // cameras the dashboard knows about, and is always loaded. SSHFS archive
  // data is keyed only by camera code, so the station being offline must
  // not blank out the Archive tab.
  const fromSettings = Object.keys((window.settingsData[host] || {}).stations || {});
  if (fromSettings.length) return fromSettings;
  const meta = (typeof state.STATIONS_META !== 'undefined' && state.STATIONS_META[host]) || null;
  return meta && Array.isArray(meta.cameras) ? meta.cameras.map(c => c.code) : [];
}

// A camera's nights, or throws: a failed load (504 while the archive is slow)
// must not be cached or shown as "no nights", which is what made the tab
// look dead when nights_full timed out.
async function _fetchNights(cam) {
  const r = await fetch(withHidden(`/api/archive/nights_full/${cam}`));
  if (!r.ok) throw new Error(r.status === 504 ? 'the archive took too long to answer' : `HTTP ${r.status}`);
  return r.json();
}

function _archiveError(el, cam, e) {
  el.innerHTML = `<div class="offline">Couldn't load ${escHtml(cam)}'s archive: ${escHtml(e?.message || e)}.
    <button type="button" class="sa-btn" onclick="archiveSelectCamera('${escHtml(cam)}')">Try again</button></div>`;
}

function _prefetchOtherCameras(cameras, currentCam) {
  for (const c of cameras) {
    if (c === currentCam || archiveState.data[c]) continue;
    _fetchNights(c).then(d => { archiveState.data[c] = d; }).catch(() => {});
  }
}

async function archiveInit() {
  const host = state.activeStation;
  const el = document.getElementById('pane-archive');

  // Kick off a settings fetch in the background — if it arrives we'll
  // pick up any RMS-side cameras the dashboard config doesn't know about
  // on the next archiveInit pass — but don't gate render on it.
  if (!window.settingsData[host]) {
    fetch(`/api/settings/${host}`)
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d) window.settingsData[host] = d; })
      .catch(() => {});
  }

  const cameras = _archiveCamerasFor(host);
  if (!cameras.length) {
    el.innerHTML = '<div class="sa-card"><p class="sa-note">No cameras configured for this station.</p></div>';
    return;
  }

  // Reset if station changed
  if (archiveState.station !== host) {
    archiveState = { station: host, selectedCamera: cameras[0], data: {} };
  }
  // The camera picked on another tab (or carried by the URL) wins.
  if (state.view.cam && cameras.includes(state.view.cam)) archiveState.selectedCamera = state.view.cam;
  if (!archiveState.selectedCamera) archiveState.selectedCamera = cameras[0];

  const cam = archiveState.selectedCamera;

  // Already loaded for this camera
  if (archiveState.data[cam]) {
    archiveRender(cameras);
    _archiveShowPinnedNight(cam);
    _prefetchOtherCameras(cameras, cam);
    return;
  }

  el.innerHTML = '<div class="offline">Loading archive…</div>';
  try {
    archiveState.data[cam] = await _fetchNights(cam);
  } catch(e) {
    _archiveError(el, cam, e);
    return;
  }
  archiveRender(cameras);
  _archiveShowPinnedNight(cam);
  _prefetchOtherCameras(cameras, cam);
}

/** Scroll to the night pinned in state.view (the closest night this camera
 *  has), so a camera, tab or station switch keeps the night on screen. */
function _archiveShowPinnedNight(cam) {
  const nights = _arcNights(cam).map(n => n.date);
  const date = nearestNight(nights, state.view.date);
  if (date) archiveJumpToDate(cam, date, { pin: false });
}

async function archiveJumpToDate(cam, iso, { pin = true } = {}) {
  if (!iso) return;
  const date = iso.replace(/-/g, '');  // YYYY-MM-DD → YYYYMMDD
  if (pin) {
    state.view.date = date;
    window.stationSyncUrl?.();
  }
  // Ensure the night is rendered before scrolling
  const nights = _arcNights(cam);
  const idx = nights.findIndex(n => n.date === date);
  if (idx >= 0 && idx >= _archiveRenderedCount) {
    _archiveAppendNights(cam, nights, idx + 1 - _archiveRenderedCount);
  }
  document.querySelectorAll('#pane-archive .arc-day.picked').forEach(d => d.classList.remove('picked'));
  document.querySelector(`#pane-archive .arc-day[data-date="${date}"]`)?.classList.add('picked');
  requestAnimationFrame(() => {
    const sec = document.getElementById(`archive-night-sec-${cam}-${date}`);
    if (sec) sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
}

async function archiveSelectCamera(cam) {
  // Close any clip/plot modal left open from the previous camera —
  // tapping a different camera in the archive list shouldn't leave
  // a stale playback on top.
  _closeAllOpenModals();
  archiveState.selectedCamera = cam;
  state.view.cam = cam;
  window.stationSyncUrl?.();
  const el = document.getElementById('pane-archive');
  const cameras = _archiveCamerasFor(state.activeStation);
  if (!archiveState.data[cam]) {
    el.innerHTML = '<div class="offline">Loading…</div>';
    try {
      archiveState.data[cam] = await _fetchNights(cam);
    } catch(e) {
      _archiveError(el, cam, e);
      return;
    }
  }
  archiveRender(cameras);
  _archiveShowPinnedNight(cam);
  _prefetchOtherCameras(cameras, cam);
}

// Clips shown per night before "Show all": a 75-clip night is one click away.
const ARC_CLIPS_PREVIEW = 11;
const _arcShowAll = new Set();  // `${cam}|${date}` nights opened in full

const _DL_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4v12M6 11l6 6 6-6M5 20h14"/></svg>';

function _buildNightSection(cam, night, idx) {
  const isFirstPage = idx < NIGHTS_PER_PAGE;
  const prio = isFirstPage ? ' fetchpriority="high"' : '';
  const cards = [];

  // The timelapse leads, twice as wide as a clip.
  if (night.timelapse) {
    const tlUrl = `/api/archive/file/${cam}/${night.date}/timelapse/${encodeURIComponent(night.timelapse)}`;
    const tlStackUrl = night.timelapse_stack
      ? `/api/archive/file/${cam}/${night.date}/timelapse/${encodeURIComponent(night.timelapse_stack)}`
      : null;
    cards.push(`<div class="arc-card arc-tl">
      <button type="button" class="arc-play" aria-label="Play the timelapse of the night"
              onclick="archivePlayVideo('${tlUrl}','${escHtml(night.timelapse)}',0)">
        ${tlStackUrl ? `<img data-src="${tlStackUrl}" decoding="async" alt=""${prio} onerror="this.remove()">` : ''}
        <span class="arc-play-icon" aria-hidden="true"><svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M7 4v16l13-8z"/></svg></span>
      </button>
      <div class="arc-meta"><span class="sa-strong">Timelapse of the night</span>
        <a class="arc-dl" href="${tlUrl}" download aria-label="Download the timelapse">${_DL_ICON}</a></div>
    </div>`);
  }

  const showAll = _arcShowAll.has(`${cam}|${night.date}`);
  const listed = _arcListed(night);
  const meteors = showAll ? listed : listed.slice(0, ARC_CLIPS_PREVIEW);
  meteors.forEach(m => {
    const stackUrl = m.stack ? `/api/archive/file/${cam}/${night.date}/${m.stack_subdir || 'meteors'}/${encodeURIComponent(m.stack)}` : null;
    const videoUrl = `/api/archive/file/${cam}/${night.date}/meteors/${encodeURIComponent(m.filename)}`;
    _arcCardData.set(m.filename, { stackUrl, cam, date: night.date, meteor_time: m.meteor_time || null, hidden: !!m.hidden });
    const ts = m.filename.match(/_(\d{6})_/)?.[1] || '';
    const timeStr = ts ? `${ts.slice(0,2)}:${ts.slice(2,4)}:${ts.slice(4,6)}` : m.filename;
    // Hide as a false positive: a clip whose meteor time names its detection.
    const hideBtn = m.meteor_time
      ? hideButtonHtml(m, `data-cam="${escHtml(cam)}" data-date="${escHtml(night.date)}" data-fn="${escHtml(m.filename)}"`) : '';
    cards.push(`<div class="arc-card${m.hidden ? ' hd-hidden' : ''}">
      <button type="button" class="arc-play" aria-label="Play the meteor at ${escHtml(timeStr)} UTC"
              onclick="archivePlayVideo('${videoUrl}?format=mp4','${escHtml(m.filename)}',${m.size_mb ?? 0},${m.detection_offset_s ?? 'null'})">
        ${stackUrl ? `<img data-src="${stackUrl}" decoding="async" alt=""${prio} onerror="this.remove()">` : ''}
      </button>
      <div class="arc-meta"><span class="sa-mono sa-strong">${escHtml(timeStr)}</span>${hideBtn}
        <a class="arc-dl" href="${videoUrl}" download aria-label="Download the clip at ${escHtml(timeStr)} UTC">${_DL_ICON}</a></div>
      ${hiddenNoteHtml(m)}
    </div>`);
  });

  const n = listed.length;
  const what = [n ? `<span class="sa-mono">${n}</span> meteor clip${n !== 1 ? 's' : ''}` : '', night.timelapse ? 'the timelapse' : '']
    .filter(Boolean).join(' and ');
  const more = !showAll && n > ARC_CLIPS_PREVIEW
    ? `<button type="button" class="sa-btn arc-more" onclick="archiveShowAllClips('${escHtml(cam)}','${night.date}')">Show all ${n} clips</button>` : '';
  return `<section class="sa-card arc-night" id="archive-night-sec-${cam}-${night.date}" aria-labelledby="arc-h-${cam}-${night.date}">
    <div class="arc-night-head"><h2 id="arc-h-${cam}-${night.date}">${nightTitle(night.date)}</h2><span class="sa-note">${what}</span></div>
    <div class="arc-grid">${cards.join('')}</div>${more}
  </section>`;
}

function archiveShowAllClips(cam, date) {
  _arcShowAll.add(`${cam}|${date}`);
  const sec = document.getElementById(`archive-night-sec-${cam}-${date}`);
  const nights = _arcNights(cam);
  const idx = nights.findIndex(n => n.date === date);
  if (!sec || idx < 0) return;
  sec.outerHTML = _buildNightSection(cam, nights[idx], idx);
  _archiveObserveImages(document.getElementById(`archive-night-sec-${cam}-${date}`));
}

/* ── The calendar: every night of the camera's archive as one square, darker
   for more meteor clips; a click opens that night. ── */
export function archiveCalendar(nights) {
  // nights: [{date: 'YYYYMMDD', clips, timelapse}] (any order)
  if (!nights.length) return { weeks: [], months: [], max: 0, busiest: null };
  const DAY = 86400000;
  const t = d => Date.UTC(+d.slice(0, 4), +d.slice(4, 6) - 1, +d.slice(6, 8));
  const byDate = new Map(nights.map(n => [n.date, n]));
  const times = nights.map(n => t(n.date));
  const first = Math.min(...times), last = Math.max(...times);
  const start = first - ((new Date(first).getUTCDay() + 6) % 7) * DAY;   // that week's Monday
  const max = Math.max(1, ...nights.map(n => n.clips));
  const weeks = [], months = [];
  let busiest = null;
  for (let w = start, col = 0; w <= last; w += 7 * DAY, col++) {
    const days = [];
    for (let k = 0; k < 7; k++) {
      const ms = w + k * DAY;
      const date = new Date(ms).toISOString().slice(0, 10).replace(/-/g, '');
      const n = byDate.get(date);
      if (ms < first || ms > last) days.push(null);
      else days.push({ date, clips: n ? n.clips : 0, timelapse: !!(n && n.timelapse), level: n && n.clips ? Math.min(4, Math.ceil(n.clips / max * 4)) : 0 });
      if (n && (!busiest || n.clips > busiest.clips)) busiest = { date, clips: n.clips };
      if (new Date(ms).getUTCDate() === 1 && ms >= first && ms <= last) months.push({ col, ms });
    }
    weeks.push(days);
  }
  if (!months.length || months[0].col > 1) months.unshift({ col: 0, ms: first });
  return { weeks, months, max, busiest };
}

function _arcCalendarHtml(cam, nights) {
  const cal = archiveCalendar(nights.map(n => ({ date: n.date, clips: _arcListed(n).length, timelapse: !!n.timelapse })));
  const fmt = (ms, o) => new Date(ms).toLocaleDateString('en-GB', { timeZone: 'UTC', ...o });
  const dayTitle = d => `${fmt(Date.UTC(+d.date.slice(0, 4), +d.date.slice(4, 6) - 1, +d.date.slice(6, 8)), { day: 'numeric', month: 'long', year: 'numeric' })}: `
    + (d.clips ? `${d.clips} meteor clip${d.clips !== 1 ? 's' : ''}` : d.timelapse ? 'timelapse only' : 'nothing archived');
  const cols = cal.weeks.map(days => days.map(d => d
    ? `<button type="button" class="arc-day l${d.level}${d.timelapse || d.clips ? '' : ' empty'}" data-date="${d.date}" title="${escHtml(dayTitle(d))}"
         aria-label="${escHtml(dayTitle(d))}" onclick="archiveJumpToDate('${escHtml(cam)}','${d.date.slice(0, 4)}-${d.date.slice(4, 6)}-${d.date.slice(6, 8)}')"></button>`
    : '<span class="arc-day none"></span>').join('')).join('');
  const months = cal.months.map(m => `<span style="left:${m.col * 17}px">${fmt(m.ms, { month: 'short' })}</span>`).join('');
  const busiest = cal.busiest && cal.busiest.clips
    ? `<span class="arc-busiest">Busiest night: ${fmt(Date.UTC(+cal.busiest.date.slice(0, 4), +cal.busiest.date.slice(4, 6) - 1, +cal.busiest.date.slice(6, 8)), { day: 'numeric', month: 'long' })}, <span class="sa-mono">${cal.busiest.clips}</span> clips.</span>` : '';
  return `<div class="arc-cal-scroll"><div class="arc-cal">
      <span></span><div class="arc-months" aria-hidden="true">${months}</div>
      <div class="arc-dows" aria-hidden="true"><span>Mon</span><span></span><span>Wed</span><span></span><span>Fri</span><span></span><span>Sun</span></div>
      <div class="arc-days">${cols}</div>
    </div></div>
    <div class="arc-legend"><span>Fewer clips <i class="arc-day l1"></i><i class="arc-day l2"></i><i class="arc-day l3"></i><i class="arc-day l4"></i> More</span>
      <span><i class="arc-day empty"></i> Nothing archived</span>${busiest}</div>`;
}

function _archiveObserveImages(container) {
  if (!_archiveImgObserver) {
    _archiveImgObserver = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          const img = entry.target;
          if (img.dataset.src) {
            img.src = img.dataset.src;
            delete img.dataset.src;
          }
          _archiveImgObserver.unobserve(img);
        }
      }
    }, { rootMargin: '400px 0px' });
  }
  const imgs = container.querySelectorAll('img[data-src]');
  for (const img of imgs) _archiveImgObserver.observe(img);
}

function _archiveAppendNights(cam, visibleNights, count) {
  const container = document.getElementById('archive-nights-container');
  if (!container) return;
  const end = Math.min(_archiveRenderedCount + count, visibleNights.length);
  for (let i = _archiveRenderedCount; i < end; i++) {
    const div = document.createElement('div');
    div.innerHTML = _buildNightSection(cam, visibleNights[i], i);
    container.appendChild(div.firstElementChild);
  }
  _archiveRenderedCount = end;
  _archiveObserveImages(container);

  const sentinel = document.getElementById('archive-load-sentinel');
  if (sentinel) {
    if (_archiveRenderedCount >= visibleNights.length) {
      sentinel.remove();
      if (_archiveSentinelObserver) { _archiveSentinelObserver.disconnect(); _archiveSentinelObserver = null; }
    }
  }
}

function archiveRender(cameras) {
  const el = document.getElementById('pane-archive');
  if (!cameras) cameras = Object.keys(archiveState.data);
  const cam = archiveState.selectedCamera || cameras[0];

  // Tear down previous observers
  if (_archiveImgObserver) { _archiveImgObserver.disconnect(); _archiveImgObserver = null; }
  if (_archiveSentinelObserver) { _archiveSentinelObserver.disconnect(); _archiveSentinelObserver = null; }
  _archiveRenderedCount = 0;

  const camBtns = cameras.map(c =>
    `<button type="button" class="sa-mono" aria-pressed="${c === cam}" onclick="archiveSelectCamera('${c}')">${c}</button>`
  ).join('');

  const visibleNights = _arcNights(cam);
  const totalTl = visibleNights.filter(n => n.timelapse).length;
  const minDate = visibleNights.length ? visibleNights[visibleNights.length - 1].date : '';
  const maxDate = visibleNights.length ? visibleNights[0].date : '';
  const toIso = d => d ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}` : '';
  const camE = escHtml(cam);
  const since = minDate ? new Date(`${toIso(minDate)}T12:00:00Z`).toLocaleDateString('en-GB', { day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' }) : '';

  const head = `<section class="sa-card arc-top" aria-labelledby="arc-title">
    <div class="sa-head">
      <div class="sa-stack">
        <div class="sa-seg" role="group" aria-label="Camera">${camBtns}</div>
        <h2 id="arc-title">${escHtml(_arcTitle(cam))}</h2>
        ${visibleNights.length ? `<p class="sa-note">On the storage box since ${since}, with ${totalTl} nightly timelapse${totalTl !== 1 ? 's' : ''}.</p>` : ''}
      </div>
      <div class="arc-tools">
      ${visibleNights.length ? `<label class="dt-label">Go to night
        <input type="date" class="sa-field dt-tall" id="archive-date-picker-${camE}"
               min="${escHtml(toIso(minDate))}" max="${escHtml(toIso(maxDate))}"
               onchange="archiveJumpToDate('${camE}', this.value)"></label>` : ''}
      ${toggleHtml('arc-hidden-toggle', _arcHiddenCount(cam), 'dt-tall')}
      </div>
    </div>
    ${visibleNights.length ? _arcCalendarHtml(cam, visibleNights) : ''}
  </section>`;

  if (!visibleNights.length) {
    el.innerHTML = `<div class="arc-root">${head}</div>`;
    _arcWireHidden(el);
    return;
  }

  const initialCount = Math.min(NIGHTS_PER_PAGE, visibleNights.length);
  const initialSections = [];
  for (let i = 0; i < initialCount; i++) {
    initialSections.push(_buildNightSection(cam, visibleNights[i], i));
  }
  _archiveRenderedCount = initialCount;

  const sentinelHtml = visibleNights.length > initialCount
    ? '<div id="archive-load-sentinel" style="height:1px"></div>'
    : '';

  el.innerHTML = `<div class="arc-root">${head}<div id="archive-nights-container" class="arc-nights">${initialSections.join('')}</div>${sentinelHtml}</div>`;
  _arcWireHidden(el);

  _archiveObserveImages(el);
  // The calendar opens on its newest weeks (it scrolls sideways on a phone).
  const calScroll = el.querySelector('.arc-cal-scroll');
  if (calScroll) calScroll.scrollLeft = calScroll.scrollWidth;

  const sentinel = document.getElementById('archive-load-sentinel');
  if (sentinel) {
    _archiveSentinelObserver = new IntersectionObserver((entries) => {
      if (entries[0].isIntersecting) {
        _archiveAppendNights(cam, visibleNights, NIGHTS_PER_PAGE);
      }
    }, { rootMargin: '600px 0px' });
    _archiveSentinelObserver.observe(sentinel);
  }
}

/* ── Hidden detections on the Archive tab ─────────────────────────────── */

/** Wire the rendered tab: the "Show hidden" toggle in the camera's toolbar
 *  (the archive lists come from the server with or without hidden clips, so
 *  a flip refetches) and the Hide buttons on the clip cards (one delegated
 *  listener on the pane, which survives re-renders). */
function _arcWireHidden(el) {
  wireToggle(document.getElementById('arc-hidden-toggle'), () => {
    archiveState.data = {};
    archiveInit();
  });
  if (el.dataset.hdWired) return;
  el.dataset.hdWired = '1';
  el.addEventListener('click', (e) => {
    const btn = e.target.closest('.arc-card .hd-btn');
    if (!btn) return;
    e.stopPropagation();
    const { cam, date, fn } = btn.dataset;
    const m = _arcCardData.get(fn);
    if (!m?.meteor_time) return;
    toggleHide(hideTarget({ meteor_time: m.meteor_time }, cam), btn.dataset.hd === 'hide',
      resp => _arcOnHiddenChange(cam, date, fn, resp), { page: 'archive' });
  });
}

/** A hide or unhide of the archived clip ``fn`` (card, player or Undo): mark
 *  it, keep the night's count, redraw that night's section and the toggle. */
function _arcOnHiddenChange(cam, date, fn, resp) {
  const night = (archiveState.data[cam] || []).find(n => n.date === date);
  const m = night?.meteors.find(x => x.filename === fn);
  if (!m || !resp) return;
  const was = !!m.hidden;
  applyHidden(m, resp);
  if (was !== !!m.hidden) night.hidden_count = Math.max(0, (night.hidden_count || 0) + (m.hidden ? 1 : -1));
  const extra = _arcCardData.get(fn);
  if (extra) extra.hidden = !!m.hidden;
  if (archiveState.selectedCamera !== cam) return;
  updateToggle(document.getElementById('arc-hidden-toggle'), _arcHiddenCount(cam));
  const title = document.getElementById('arc-title');
  if (title) title.textContent = _arcTitle(cam);
  const sec = document.getElementById(`archive-night-sec-${cam}-${date}`);
  if (!sec) return;
  if (!_arcListed(night).length && !night.timelapse) { sec.remove(); return; }
  const idx = _arcNights(cam).findIndex(n => n.date === date);
  sec.outerHTML = _buildNightSection(cam, night, Math.max(0, idx));
  _archiveObserveImages(document.getElementById(`archive-night-sec-${cam}-${date}`));
}

/* ── Archive modal ─────────────────────────────────────────────────────── */
const _arcModal = new VideoModal(
  document.getElementById('arc-modal-container'),
  { trim: true, nav: false }
);
// Register global close-all handler (set once; may already be set by overview.js)
if (!window._VideoModalCloseAll) window._VideoModalCloseAll = () => VideoModal.closeAll();

async function archivePlayVideo(url, filename, sizeMb, detOffset) {
  const extra = _arcCardData.get(filename) || {};
  const ts = filename.match(/_(\d{6})_/)?.[1] || '';
  const timeStr = ts ? `${ts.slice(0,2)}:${ts.slice(2,4)}:${ts.slice(4,6)} UTC` : filename;
  const title = [extra.cam, timeStr].filter(Boolean).join(' · ');

  // Fetch RMS detections for this night and match by meteor_time (truncated to second)
  let detection = extra.meteor_time ? { time_utc: extra.meteor_time, camera: extra.cam } : null;
  if (extra.cam && extra.date && extra.meteor_time) {
    const meteortSec = utcSecondKey(extra.meteor_time); // "2026-06-09T19:53:14"
    try {
      const r = await fetch(`/api/archive/rms-detections/${extra.cam}/${extra.date}`);
      if (r.ok) {
        const data = await r.json();
        const match = data.detections?.find(d => utcSecondKey(d.time_utc) === meteortSec);
        if (match) detection = { ...match, camera: extra.cam };
      }
    } catch (_) {}
  }

  _arcModal.open({
    src:       url,
    title:     title || filename,
    station:   state.activeStation,
    camera:    extra.cam   || '',
    date:      extra.date  || '',
    filename,
    // Archived clips are locked detections by definition — show the marker.
    locked:    true,
    lockType:  'detection',
    loopDlPath: extra.cam && extra.date
      ? `/loop_clip_archive/${extra.cam}/${extra.date}/${encodeURIComponent(filename)}`
      : null,
    detOffset: detOffset ?? null,
    // "Download clip" = the whole archived clip as MP4 (the uploaded .mp4
    // copy, else a stream-copy remux); the trim handles drive the separate
    // "Download trimmed".
    download:  {
      originalUrl: extra.cam && extra.date
        ? `/api/archive/file/${extra.cam}/${extra.date}/meteors/${encodeURIComponent(filename)}`
          + (/\.mkv$/.test(filename) ? '?format=mp4' : '')
        : null,
      onClick: (btn) => _arcTrimDownload(btn),
    },
    stack:     extra.stackUrl ? { url: extra.stackUrl } : null,
    detection,
    // Hide as a false positive: an archived meteor clip (never the
    // timelapse, which has no meteor time). The RMS row's key when it was
    // found, else the camera and meteor time.
    hide: extra.cam && extra.meteor_time ? {
      target: hideTarget(detection?.ff_file ? detection : { meteor_time: extra.meteor_time }, extra.cam),
      hidden: !!extra.hidden,
      page: 'archive',
      onChange: resp => _arcOnHiddenChange(extra.cam, extra.date, filename, resp),
    } : null,
  });
}

// Trim-aware download: honours the modal's start/end handles and takes the
// filename from the server's Content-Disposition (via a blob URL, so the
// download attribute is always respected — an <a download> on the raw clip
// URL both ignored the trim and lost the name on cross-origin/inline responses).
async function _arcTrimDownload(btn) {
  const ts = _arcModal.getTrimState();
  if (!ts) return;
  const { camera, date, filename } = ts;
  if (!camera || !date || !filename) return;
  const dlBtn = btn || _arcModal._el?.querySelector('.vm-download-btn');
  const label = dlBtn ? dlBtn.innerHTML : null;
  if (dlBtn) { dlBtn.disabled = true; dlBtn.textContent = 'Preparing…'; }
  try {
    const base = `/api/archive/shortclip/${camera}/${date}/${encodeURIComponent(filename)}`;
    const { url } = buildTrimmedShortclipUrl(base, ts);
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const cd = resp.headers.get('content-disposition') || '';
    const m  = cd.match(/filename="?([^";\n]+)"?/);
    saveBlob(await resp.blob(), m ? m[1] : 'clip.mp4');
  } catch (e) {
    alert('Download failed: ' + e.message);
  } finally {
    if (dlBtn) { dlBtn.disabled = false; dlBtn.innerHTML = label; }
  }
}

function arcModalClose(e) {
  // Legacy inline onclick handler — keep for any remaining callers
  if (e && e.target && !e.target.closest('.vm-backdrop')) return;
  _arcModal.close();
}

// Expose to global scope for callers in dashboard-station.js
window.arcModalClose    = arcModalClose;
window.archivePlayVideo = archivePlayVideo;
window.archiveSelectCamera = archiveSelectCamera;
window.archiveInit = archiveInit;
window.archiveJumpToDate = archiveJumpToDate;
window.archiveShowAllClips = archiveShowAllClips;
