import { fmtBytes, fmtDate, parseFn, fetchJson, _modalOpen, _modalClose, escHtml, state, showerFullName, nearestNight, sameClip, utcSecondKey, nightTitle } from './dashboard-common.js';
import { withHidden, showingHidden, toggleHtml, wireToggle, updateToggle, hideButtonHtml, hiddenNoteHtml, hideTarget, toggleHide, applyHidden } from './dashboard-hidden.js';
/*
 * dashboard-rms.js
 * ────────────────
 * RMS / Detection tab — sky-dome thumb, calendar night-picker, shower/sort filter, locked-clip grid, processing plots, plot zoom modal helpers. Loaded by dashboard.html before dashboard-station.js.
 *
 * Depends on dashboard-common.js (escHtml, fmtBytes, fmtDate, parseFn, state.STATIONS_META,
 * state.VDB_CAMERAS, state.statusData, state.vitalsData, state.tlData, state.activeStation, state.activeTab, state.IS_ADMIN,
 * state.AUTH_USER, state.USER_ROLE, state.USER_STATIONS, _modalOpen, _modalClose) and lexical bindings
 * declared at the top level of dashboard-station.js (classic non-module scripts share
 * the global lexical environment, so top-level `let`/`const` are visible across bundles).
 */

/* ─────────────────────────────────────────
   RMS tab render
───────────────────────────────────────── */
window.rmsState = { station: null, selectedCamera: null };

// One-shot scroll target consumed by fetchRMSAllNights once the night
// sections are rendered. Used by the ?date= URL param so a deep-link from
// the overview's GMN popup (or anywhere else) lands the user on the
// requested night instead of the top of the list.
window._rmsPendingScrollDate = null;

function renderRMS(host) {
  const el      = document.getElementById('pane-rms');
  const cameras = state.VDB_CAMERAS[host] || [];
  if (!cameras.length) {
    el.innerHTML = '<div class="no-data">No cameras configured for this station</div>';
    return;
  }

  if (window.rmsState.station !== host) {
    window.rmsState = { station: host, selectedCamera: cameras[0] };
  }
  // A camera picked on another tab (or carried by the URL) wins over this
  // tab's own last pick, so switching tabs never changes the camera.
  if (state.view.cam && cameras.includes(state.view.cam)) {
    window.rmsState.selectedCamera = state.view.cam;
  }
  if (!window.rmsState.selectedCamera || !cameras.includes(window.rmsState.selectedCamera)) {
    window.rmsState.selectedCamera = cameras[0];
  }
  const cam = window.rmsState.selectedCamera;

  const camBtns = cameras.map(c =>
    `<button type="button" class="sa-mono" aria-pressed="${c === cam}" onclick="rmsSelectCamera('${host}','${c}')">${c}</button>`
  ).join('');

  el.dataset.host = host;
  // One toolbar (camera, nights, shower, sort, size), then the nights. The
  // live sky dome is on the Video DB tab only.
  el.innerHTML = `
    <div class="dt-root" id="rms-sec-${host}-${cam}">
      <div class="dt-bar">
        <div class="sa-seg rms-cam-seg" role="group" aria-label="Camera">${camBtns}</div>
        <div class="rms-cal-wrap" id="rms-cal-wrap-${host}-${cam}">
          <button class="sa-btn dt-tall rms-cal-btn" id="rms-cal-btn-${host}-${cam}" aria-haspopup="dialog"
                  onclick="rmsCalToggle('${host}','${cam}', event)" type="button">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/></svg>
            <span id="rms-cal-label-${host}-${cam}">All nights</span>
          </button>
          <div class="rms-cal-pop" id="rms-cal-pop-${host}-${cam}" hidden></div>
        </div>
        <div class="sa-seg dt-showers" role="group" aria-label="Shower" id="rms-shower-btns-${host}-${cam}">
          <button class="rms-flt-btn active" aria-pressed="true" data-shower="all"
                  onclick="rmsSelectShower('${host}','${cam}','all')">All</button>
        </div>
        <label class="dt-label">Sort
          <select class="sa-field dt-tall" id="rms-sort-${host}-${cam}" onchange="rmsApplyFilter('${host}','${cam}')">
            <option value="date-desc">Newest first</option>
            <option value="date-asc">Oldest first</option>
            <option value="mag-asc">Brightest first</option>
            <option value="mag-desc">Faintest first</option>
          </select>
        </label>
        <button type="button" class="sa-btn dt-tall" onclick="rmsClearFilter('${host}','${cam}')">Reset</button>
        ${toggleHtml(`rms-hidden-${host}-${cam}`, 0, 'dt-tall')}
        <label class="dt-label dt-size">Size
          <input type="range" class="rms-zoom" id="rms-zoom-${host}-${cam}" min="120" max="960" value="220"
                 oninput="rmsUpdateZoom('${host}','${cam}',this.value)">
        </label>
      </div>
      <p class="sa-note dt-count"><span id="rms-count-${host}-${cam}"></span> <span id="rms-flt-summary-${host}-${cam}"></span></p>
      <div class="rms-chunk-area dt-nights" id="rms-chunks-${host}-${cam}">
        <div class="offline">Loading…</div>
      </div>
    </div>`;

  // "Show hidden": the lists come from the server with or without the
  // detections people hid as false positives, so a flip refetches them.
  wireToggle(document.getElementById(`rms-hidden-${host}-${cam}`), () => fetchRMSAllNights(host, cam));
  fetchRMSAllNights(host, cam);
}

function rmsSelectCamera(host, cam) {
  // Carry the filters and the night in view over to the new camera; only
  // the camera changes.
  _rmsCarry = rmsCaptureView();
  window.rmsState.selectedCamera = cam;
  state.view.cam = cam;
  renderRMS(host);
  window.stationSyncUrl?.();
}

/* The pane state a camera switch must not lose, captured just before the
   pane is rebuilt and re-applied once the new camera's nights have rendered
   (fetchRMSAllNights). */
let _rmsCarry = null;
const RMS_STICKY_HEADER_PX = 90;

/** Date of the night section the user has scrolled to, or null while they
 *  are still above the night list (then there is nothing to return to). */
function _rmsNightInView(host, cam) {
  const secs = [...document.querySelectorAll(`[id^="rms-night-sec-${host}-${cam}-"]`)]
    .filter(sec => sec.offsetParent !== null);
  if (!secs.length || secs[0].getBoundingClientRect().top >= RMS_STICKY_HEADER_PX) return null;
  const sec = secs.find(x => x.getBoundingClientRect().bottom > RMS_STICKY_HEADER_PX);
  return sec ? sec.id.slice(-8) : null;
}

/** Snapshot the RMS pane (filters, zoom, calendar picks, night in view) and
 *  pin the night in view into state.view so other tabs open it too. Called
 *  on a camera switch and by the station page before leaving the tab. */
function rmsCaptureView() {
  const { station: host, selectedCamera: cam } = window.rmsState;
  if (!host || !cam || !document.getElementById(`rms-sec-${host}-${cam}`)) return null;
  const date = _rmsNightInView(host, cam);
  if (date) state.view.date = date;
  const shower = _rmsActiveShower(host, cam);
  return {
    showers: shower.all ? null : new Set(shower.set),
    sort: _rmsActiveSort(host, cam),
    zoom: document.getElementById(`rms-zoom-${host}-${cam}`)?.value || null,
    nights: new Set(_rmsCalSelected),
    scrollDate: date,
  };
}
window.rmsCaptureView = rmsCaptureView;

function rmsJumpToNight(host, cam, date) {
  if (!date) return;
  if (!_rmsExpanded.has(date) && document.getElementById(`rms-night-sec-${host}-${cam}-${date}`)?.dataset.collapsed) {
    _rmsExpanded.add(date);
    renderRMSAllNights(host, cam, _rmsResults);
  }
  // Each night section has id=`rms-night-sec-<host>-<cam>-<date>` (set by
  // renderRMSAllNights). Scroll it into view.
  const sec = document.getElementById(`rms-night-sec-${host}-${cam}-${date}`);
  if (sec) sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

let _rmsAllChunks = [];
let _rmsDetections = {};  // chunk prefix (STATION_DATE_TIME) → RMS detection metadata
let _rmsResults = [];     // [{date, chunks}] from last fetch — cached for re-filter without refetch
let _rmsVisibleChunks = []; // current filtered+sorted set (modal nav cycles through this)
let _rmsNightDets = new Map(); // date → that night's RMS detections, for the strip and numbers
let _rmsExpanded = new Set();  // nights the user opened; the newest is always open
let _rmsTwilight = null;       // one night's twilight, for the strips' sky
const _rmsPlotsAsked = new Set();
let _rmsFetchSeq = 0;       // monotonic guard: only the latest fetch may commit globals/DOM

const RMS_FANOUT_LIMIT = 3;   // nights in flight; two requests each

/** Promise.all with at most `limit` calls of `fn` in flight; order preserved. */
async function mapWithLimit(items, limit, fn) {
  const out = new Array(items.length);
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const i = next++;
      out[i] = await fn(items[i], i);
    }
  };
  const n = Math.max(1, Math.min(limit, items.length));
  await Promise.all(Array.from({ length: n }, worker));
  return out;
}

// A camera's nights render as they arrive, newest first: as soon as the first
// clips are in, then at 10 and 30 nights, then complete. A night the dashboard
// has not listed yet costs a Storage Box round trip, and waiting for all of a
// camera's ~90 nights kept the tab blank for minutes (gmnro09 (Sibiciu),
// 2026-09-27). Few steps, because each render replaces the list.
const RMS_RENDER_STEPS = [10, 30];

// Plot galleries by `${host}|${cam}|${date}`, so a re-render (a later step, a
// filter change) refills them instead of leaving them empty.
const _rmsPlots = new Map();

async function fetchRMSAllNights(host, cam) {
  const token = ++_rmsFetchSeq;
  const el = document.getElementById(`rms-chunks-${host}-${cam}`);
  if (!el) return;
  el.innerHTML = '<div class="offline">Loading…</div>';
  // Clear stale detection metadata from a previous camera so it doesn't
  // persist if this fetch fails or returns different data.
  _rmsDetections = {};
  _rmsNightDets = new Map();
  _rmsExpanded = new Set();
  _rmsTwilight = null;
  _rmsPlots.clear();
  _rmsPlotsAsked.clear();
  const apiHost = window.hostForCamera(host, cam);
  try {
    const resp = await fetch(`/api/videodb/rmsnights/${apiHost}/${cam}`);
    if (token !== _rmsFetchSeq) return;
    if (!resp.ok) throw new Error(resp.status === 401 ? 'Session expired — please refresh' : resp.status === 403 ? 'You do not have access to this station' : `Server error (${resp.status})`);
    const nights = await resp.json();
    if (token !== _rmsFetchSeq) return;
    if (!nights.length) {
      el.innerHTML = '<div class="no-data">No recordings found</div>';
      return;
    }
    const carry = _rmsCarry;
    _rmsCarry = null;
    if (carry) {
      const sortEl = document.getElementById(`rms-sort-${host}-${cam}`);
      if (sortEl && carry.sort) sortEl.value = carry.sort;
      const zoomEl = document.getElementById(`rms-zoom-${host}-${cam}`);
      if (zoomEl && carry.zoom) zoomEl.value = carry.zoom;
    }
    // A camera switch keeps the calendar and shower picks this camera also
    // has; they are re-applied at each step until every pick has shown up.
    let pendingNights = carry ? new Set(carry.nights) : null;
    let pendingShowers = carry ? carry.showers : undefined;
    const wanted = window._rmsPendingScrollDate || (carry && carry.scrollDate) || null;
    window._rmsPendingScrollDate = null;
    let scrolled = !wanted;
    let rendered = 0;

    const commit = (n, final) => {
      const done = perNight.slice(0, n);
      const results = done.map(([res]) => res);
      // Build detection lookup: time_utc cut to the second → detection
      _rmsDetections = {};
      _rmsNightDets = new Map();
      let hiddenCount = 0;
      for (const [res, data] of done) {
        hiddenCount += data?.hidden_count || 0;
        const dets = (data?.detections || []).filter(det => det.time_utc);
        _rmsNightDets.set(res.date, dets);
        for (const det of dets) _rmsDetections[utcSecondKey(det.time_utc)] = det;
      }
      // Build flat nav list with per-chunk date
      _rmsAllChunks = [];
      results.forEach(({ date, chunks }) =>
        chunks.forEach(c => _rmsAllChunks.push({ ...c, _date: date }))
      );
      _rmsVisibleChunks = _rmsAllChunks;
      updateToggle(document.getElementById(`rms-hidden-${host}-${cam}`), hiddenCount);
      const total = _rmsAllChunks.length;
      const countEl = document.getElementById(`rms-count-${host}-${cam}`);
      if (countEl) {
        const clips = `${total} clip${total !== 1 ? 's' : ''}`;
        countEl.textContent = final
          ? `${nights.length} night${nights.length !== 1 ? 's' : ''}, ${clips}`
          : `${n} of ${nights.length} nights, ${clips} so far`;
      }
      _rmsResults = results;
      if (!rendered) {
        _rmsCalViewMonth = null;
        _rmsCalSelected = new Set();
      }
      _rmsCalRefreshAvailable();
      if (pendingNights) {
        _rmsCalSelected = new Set([...pendingNights].filter(d => _rmsCalAvailable.has(d)));
        if (final || _rmsCalSelected.size === pendingNights.size) pendingNights = null;
      }
      _rmsCalUpdateLabel(host, cam);
      _rmsPopulateShowerOptions(host, cam, pendingShowers);
      if (pendingShowers !== undefined) {
        const seen = new Set(Object.values(_rmsDetections).map(d => d && d.shower));
        if (final || !pendingShowers || [...pendingShowers].every(sh => seen.has(sh))) {
          pendingShowers = undefined;
        }
      }
      _rmsRenderKeepingPlace(host, cam, results);
      rendered = n;
      if (!final) {
        el.insertAdjacentHTML('beforeend',
          `<div class="offline rms-more" id="rms-more-${host}-${cam}">Loading older nights… ${n} of ${nights.length}</div>`);
      }
      if (_rmsTwilight === null && results[0]) {
        _rmsTwilight = false;  // asked once per camera
        const tonight = results[0].date;
        fetch(`/api/twilight/${encodeURIComponent(host)}/${tonight}`).then(r => r.ok ? r.json() : null)
          .then(tw => { if (tw && token === _rmsFetchSeq) { _rmsTwilight = tw; _rmsPaintSky(host, cam); } })
          .catch(() => {});
      }
      // Land on the night the user was looking at (a ?date= deep-link or a
      // night pinned on another tab, else the night in view before a camera
      // switch) once it is in; at the end, the closest night this camera has.
      // Scrolling happens on the next frame so the just-rendered DOM has
      // committed layout (otherwise scrollIntoView() snaps to the wrong y).
      if (!scrolled) {
        const withClips = results.filter(r => r.chunks && r.chunks.length).map(r => r.date);
        const target = withClips.includes(wanted) ? wanted
          : (final ? nearestNight(withClips, wanted) : null);
        if (target) {
          scrolled = true;
          requestAnimationFrame(() => rmsJumpToNight(host, cam, target));
        }
      }
      // ?clip=<filename>: open that clip's player once its card is rendered.
      if (window._pendingClip) {
        const idx = _rmsVisibleChunks.findIndex(c => sameClip(c.filename, window._pendingClip));
        if (idx >= 0) {
          window._pendingClip = null;
          requestAnimationFrame(() => rmsOpenModal(idx));
        }
      }
    };

    // Two requests per night, for every night of the camera. Over HTTP/2 an
    // unbounded Promise.all put all of them on the server at once (200+ for
    // an old camera), which pinned every worker thread on the storage box
    // and 504ed unrelated pages. A small in-flight cap keeps the tab just as
    // fast (the server answers from cache) without the stampede.
    // Both requests for a night share ONE limiter: two independent limiters put
    // 2 x RMS_FANOUT_LIMIT on the server, each an archive scan holding one of
    // the storage box's six read permits, which starved every other tab (the
    // Spectro listing 504ed behind it on 2026-09-23).
    const perNight = new Array(nights.length);
    let ready = 0;   // leading nights whose two requests have both finished
    await mapWithLimit(nights, RMS_FANOUT_LIMIT, async (date, i) => {
      // A newer fetch (another camera, another tab) took over: stop asking.
      if (token !== _rmsFetchSeq) return;
      perNight[i] = await Promise.all([
        fetchJson(withHidden(`/api/videodb/chunks/${apiHost}/${cam}/${date}?locked_only=1`))
          .then(raw => ({ date, chunks: Array.isArray(raw) ? raw : (raw.chunks || []) }))
          .catch(() => ({ date, chunks: [] })),
        // RMS detection metadata (mag, shower, radiant)
        fetch(withHidden(`/api/videodb/rms-detections/${apiHost}/${cam}/${date}`))
          .then(r => r.ok ? r.json() : null)
          .catch(() => null),
      ]);
      if (token !== _rmsFetchSeq) return;
      while (ready < nights.length && perNight[ready]) ready++;
      if (ready === nights.length || ready <= rendered) return;
      const anyClips = perNight.slice(0, ready).some(([res]) => res.chunks.length);
      if (anyClips && (!rendered || RMS_RENDER_STEPS.some(st => rendered < st && ready >= st))) {
        commit(ready, false);
      } else {
        const more = document.getElementById(`rms-more-${host}-${cam}`);
        const text = `${rendered ? 'Loading older nights' : 'Loading'}… ${ready} of ${nights.length}`;
        if (more) more.textContent = text;
        else if (!rendered) el.innerHTML = `<div class="offline">${text}</div>`;
      }
    });
    if (token !== _rmsFetchSeq) return;
    commit(nights.length, true);
  } catch(e) {
    if (token !== _rmsFetchSeq) return;
    el.innerHTML = `<div class="offline">Error: ${escHtml(e?.message || e)}</div>`;
  }
}

/** Re-render the list keeping the night at the top of the viewport where it
 *  is: a later step adds nights below it (above it, oldest first). */
function _rmsRenderKeepingPlace(host, cam, results) {
  const date = _rmsNightInView(host, cam);
  const secId = date && `rms-night-sec-${host}-${cam}-${date}`;
  const before = secId ? document.getElementById(secId)?.getBoundingClientRect().top : null;
  renderRMSAllNights(host, cam, results);
  if (before == null) return;
  const after = document.getElementById(secId)?.getBoundingClientRect().top;
  if (after != null && after !== before) window.scrollBy(0, after - before);
}

/** Look up RMS detection metadata for a chunk using its meteor_time, matched
 *  to the same second (see utcSecondKey). */
function _lookupDetection(filename, meteorTime) {
  if (!meteorTime) return null;
  return _rmsDetections[utcSecondKey(meteorTime)] || null;
}

/** The speed a card shows for a GMN trajectory: the entry speed (Vinit, what
 *  the meteor hit the atmosphere at), never the geocentric Vg. Vg removes
 *  Earth's gravity, so a slow meteoroid reads as low as 4.7 km/s, which looks
 *  impossible next to the 11.2 km/s minimum entry speed; it stays in the
 *  modal's detail panel. Empty when GMN gave no entry speed. */
export function entrySpeedText(traj) {
  const v = traj?.velocity_init_km_s;
  return v != null && Number.isFinite(v) ? `${v.toFixed(1)} km/s` : '';
}

// ── RMS card filter (per-camera shower / sort buttons) ────────────────────

// Returns { all: true, set: null } when "All" is selected (no filter),
// or { all: false, set: Set<shower-code> } when one or more specific showers are picked.
function _rmsActiveShower(host, cam) {
  const row = document.getElementById(`rms-shower-btns-${host}-${cam}`);
  if (!row) return { all: true, set: null };
  const active = [...row.querySelectorAll('.rms-flt-btn.active')]
    .map(b => b.dataset.shower);
  if (!active.length || active.includes('all')) return { all: true, set: null };
  return { all: false, set: new Set(active) };
}

function _rmsActiveSort(host, cam) {
  const sel = document.getElementById(`rms-sort-${host}-${cam}`);
  return sel?.value || 'date-desc';
}

function _rmsReadFilter(host, cam) {
  return {
    shower: _rmsActiveShower(host, cam),
    sort: _rmsActiveSort(host, cam),
    nights: _rmsCalSelected,
  };
}

// ── Calendar night picker (multi-select with click/drag) ──────────────────────

let _rmsCalSelected = new Set();   // YYYYMMDD strings; empty = all nights
let _rmsCalViewMonth = null;       // 'YYYY-MM'
let _rmsCalDragMode = null;        // 'add' | 'remove' | null
let _rmsCalDragOriginal = null;    // Set snapshot at drag start (for cancel)
let _rmsCalAvailable = new Set();  // YYYYMMDD strings that have data

function _rmsCalAvailableMonths() {
  const months = new Set();
  for (const d of _rmsCalAvailable) months.add(`${d.slice(0,4)}-${d.slice(4,6)}`);
  return [...months].sort();
}

function _rmsCalUpdateLabel(host, cam) {
  const lbl = document.getElementById(`rms-cal-label-${host}-${cam}`);
  if (!lbl) return;
  const n = _rmsCalSelected.size;
  lbl.textContent = n === 0 ? 'All nights' : (n === 1 ? '1 night' : `${n} nights`);
  const btn = document.getElementById(`rms-cal-btn-${host}-${cam}`);
  if (btn) btn.classList.toggle('active', n > 0);
}

function _rmsCalRefreshAvailable() {
  _rmsCalAvailable = new Set();
  for (const night of _rmsResults || []) {
    if (night.chunks && night.chunks.length) _rmsCalAvailable.add(night.date);
  }
}

function _rmsCalBuildMonth(host, cam) {
  const [y, m] = _rmsCalViewMonth.split('-').map(Number);
  // Cells: leading blanks (Mon=1) + 28-31 days + trailing blanks
  const firstDow = (new Date(Date.UTC(y, m-1, 1)).getUTCDay() + 6) % 7; // Mon=0
  const lastDay = new Date(Date.UTC(y, m, 0)).getUTCDate();
  const cells = [];
  for (let i = 0; i < firstDow; i++) cells.push(null);
  for (let d = 1; d <= lastDay; d++) {
    const ymd = `${y}${String(m).padStart(2,'0')}${String(d).padStart(2,'0')}`;
    cells.push(ymd);
  }
  while (cells.length % 7 !== 0) cells.push(null);
  const months = _rmsCalAvailableMonths();
  const idx = months.indexOf(_rmsCalViewMonth);
  const prevMonth = idx > 0 ? months[idx-1] : null;
  const nextMonth = (idx >= 0 && idx < months.length-1) ? months[idx+1] : null;
  const monthLabel = new Date(Date.UTC(y, m-1, 1))
    .toLocaleString('en-US', { month: 'long', year: 'numeric', timeZone: 'UTC' });
  const dows = ['Mo','Tu','We','Th','Fr','Sa','Su'];
  const grid = cells.map(ymd => {
    if (!ymd) return '<span class="rms-cal-cell rms-cal-empty"></span>';
    const day = parseInt(ymd.slice(6,8));
    const has = _rmsCalAvailable.has(ymd);
    const sel = _rmsCalSelected.has(ymd);
    const cls = ['rms-cal-cell'];
    if (!has) cls.push('rms-cal-disabled');
    if (sel) cls.push('rms-cal-selected');
    return `<span class="${cls.join(' ')}" data-ymd="${ymd}"
              ${has ? `onmousedown="rmsCalDayDown('${host}','${cam}','${ymd}',event)"
                       onmouseenter="rmsCalDayEnter('${host}','${cam}','${ymd}')"` : ''}
            >${day}</span>`;
  }).join('');
  const navLeft  = prevMonth ? `onclick="rmsCalNav('${host}','${cam}','${prevMonth}')"` : 'disabled';
  const navRight = nextMonth ? `onclick="rmsCalNav('${host}','${cam}','${nextMonth}')"` : 'disabled';
  const selN = _rmsCalSelected.size;
  return `
    <div class="rms-cal-head">
      <button type="button" class="rms-cal-nav" aria-label="Previous month" ${navLeft}>&#9664;</button>
      <span class="rms-cal-month">${monthLabel}</span>
      <button type="button" class="rms-cal-nav" aria-label="Next month" ${navRight}>&#9654;</button>
    </div>
    <div class="rms-cal-dows">${dows.map(d => `<span>${d}</span>`).join('')}</div>
    <div class="rms-cal-grid">${grid}</div>
    <div class="rms-cal-foot">
      <span class="rms-cal-count">${selN === 0 ? 'No nights selected (= all)' : `${selN} selected`}</span>
      <button type="button" class="rms-cal-action" onclick="rmsCalClearSelection('${host}','${cam}')">Clear</button>
      <button type="button" class="rms-cal-action rms-cal-go" onclick="rmsCalGo('${host}','${cam}')">Go</button>
    </div>`;
}

function _rmsCalRender(host, cam) {
  const pop = document.getElementById(`rms-cal-pop-${host}-${cam}`);
  if (!pop) return;
  pop.innerHTML = _rmsCalBuildMonth(host, cam);
}

let _rmsCalEscHandler = null;  // stored reference for cleanup

function _rmsCalClose(pop) {
  if (!pop || pop.hidden) return;
  pop.hidden = true;
  if (_rmsCalEscHandler) {
    document.removeEventListener('keydown', _rmsCalEscHandler);
    _rmsCalEscHandler = null;
  }
}

function rmsCalToggle(host, cam, ev) {
  if (ev) ev.stopPropagation();
  const pop = document.getElementById(`rms-cal-pop-${host}-${cam}`);
  if (!pop) return;
  if (!pop.hidden) { _rmsCalClose(pop); return; }
  _rmsCalRefreshAvailable();
  if (!_rmsCalAvailable.size) {
    pop.innerHTML = '<div class="rms-cal-empty-msg">No nights with data yet</div>';
    pop.hidden = false;
    return;
  }
  // Default month: most recent night, or month containing first selected night
  if (!_rmsCalViewMonth || !_rmsCalAvailableMonths().includes(_rmsCalViewMonth)) {
    const months = _rmsCalAvailableMonths();
    const seedNight = _rmsCalSelected.size ? [..._rmsCalSelected].sort().pop() : [..._rmsCalAvailable].sort().pop();
    _rmsCalViewMonth = `${seedNight.slice(0,4)}-${seedNight.slice(4,6)}`;
    if (!months.includes(_rmsCalViewMonth)) _rmsCalViewMonth = months[months.length-1];
  }
  _rmsCalRender(host, cam);
  pop.hidden = false;
  // Escape key closes the popup
  if (_rmsCalEscHandler) document.removeEventListener('keydown', _rmsCalEscHandler);
  _rmsCalEscHandler = (e) => {
    if (e.key === 'Escape') { _rmsCalClose(pop); }
  };
  document.addEventListener('keydown', _rmsCalEscHandler);
}

function rmsCalNav(host, cam, monthYM) {
  _rmsCalViewMonth = monthYM;
  _rmsCalRender(host, cam);
}

let _rmsCalMouseUpHandler = null;  // stored reference for cleanup

function rmsCalDayDown(host, cam, ymd, ev) {
  ev.preventDefault();
  // Clean up any prior mouseup listener that leaked (e.g. popup closed mid-drag)
  if (_rmsCalMouseUpHandler) {
    document.removeEventListener('mouseup', _rmsCalMouseUpHandler);
    _rmsCalMouseUpHandler = null;
  }
  _rmsCalDragOriginal = new Set(_rmsCalSelected);
  _rmsCalDragMode = _rmsCalSelected.has(ymd) ? 'remove' : 'add';
  if (_rmsCalDragMode === 'add') _rmsCalSelected.add(ymd);
  else _rmsCalSelected.delete(ymd);
  _rmsCalRender(host, cam);
  // Global mouseup ends the drag
  _rmsCalMouseUpHandler = () => {
    _rmsCalDragMode = null;
    _rmsCalDragOriginal = null;
    if (_rmsCalMouseUpHandler) {
      document.removeEventListener('mouseup', _rmsCalMouseUpHandler);
      _rmsCalMouseUpHandler = null;
    }
  };
  document.addEventListener('mouseup', _rmsCalMouseUpHandler);
}

function rmsCalDayEnter(host, cam, ymd) {
  if (!_rmsCalDragMode) return;
  if (_rmsCalDragMode === 'add') _rmsCalSelected.add(ymd);
  else _rmsCalSelected.delete(ymd);
  _rmsCalRender(host, cam);
}

function rmsCalClearSelection(host, cam) {
  _rmsCalSelected.clear();
  _rmsCalRender(host, cam);
}

function rmsCalGo(host, cam) {
  const pop = document.getElementById(`rms-cal-pop-${host}-${cam}`);
  _rmsCalClose(pop);
  _rmsCalUpdateLabel(host, cam);
  // One picked night is the night the user is looking at, so the other tabs
  // (Video DB, Archive, Final Data Products) open it too.
  if (_rmsCalSelected.size === 1) {
    state.view.date = [..._rmsCalSelected][0];
    window.stationSyncUrl?.();
  }
  rmsApplyFilter(host, cam);
}

// Close popup when clicking outside
document.addEventListener('click', (e) => {
  document.querySelectorAll('.rms-cal-pop').forEach(pop => {
    if (pop.hidden) return;
    if (!pop.parentElement.contains(e.target)) _rmsCalClose(pop);
  });
});

// Shower colours: data colours, kept per camera so a shower keeps its colour
// on every night. The most frequent showers get the palette in order;
// sporadics are grey.
const _RMS_SHOWER_PALETTE = ['var(--orange)', 'var(--purple)', 'var(--blue)', 'var(--green)', '#f778ba', 'var(--yellow)'];
let _rmsShowerColors = {};

function _rmsShowerColor(code) {
  if (!code || code === 'SPO') return 'var(--muted)';
  return _rmsShowerColors[code] || 'var(--text)';
}

function _rmsPopulateShowerOptions(host, cam, carriedPicks) {
  const row = document.getElementById(`rms-shower-btns-${host}-${cam}`);
  if (!row) return;
  const counts = {};
  for (const det of Object.values(_rmsDetections)) {
    if (det && det.shower) counts[det.shower] = (counts[det.shower] || 0) + 1;
  }
  const byCount = Object.keys(counts).filter(s => s !== 'SPO').sort((a, b) => counts[b] - counts[a] || a.localeCompare(b));
  _rmsShowerColors = {};
  byCount.forEach((s, i) => { _rmsShowerColors[s] = _RMS_SHOWER_PALETTE[i % _RMS_SHOWER_PALETTE.length]; });
  // Preserve any currently-active picks that are still observed; if "All" was
  // active or nothing survives, fall back to "All". After a camera switch the
  // row is freshly rendered, so the previous camera's picks come in as
  // `carriedPicks` (null = "All" was active).
  const prevActive = carriedPicks !== undefined
    ? (carriedPicks ? new Set(carriedPicks) : new Set(['all']))
    : new Set([...row.querySelectorAll('.rms-flt-btn.active')].map(b => b.dataset.shower));
  const allWasActive = prevActive.size === 0 || prevActive.has('all');
  const survivors = [...prevActive].filter(s => s !== 'all' && counts[s]);
  const useAll = allWasActive || survivors.length === 0;
  const activeSet = useAll ? new Set(['all']) : new Set(survivors);
  const ordered = ['all', ...byCount, ...(counts.SPO ? ['SPO'] : [])];
  row.innerHTML = _rmsShowerButtonsHtml(host, cam, ordered, counts, activeSet);
}

/** The shower filter buttons: "all" plus one per observed code. A code is
 *  station-pushed text, so the handler reads it back from data-shower rather
 *  than from a JS string literal (HTML escaping does not protect one: the
 *  parser decodes &#39; back to a quote before the handler runs). */
export function _rmsShowerButtonsHtml(host, cam, ordered, counts, activeSet) {
  return ordered.map(s => {
    const on = activeSet.has(s);
    const label = s === 'all' ? 'All'
      : `<i class="dt-dot" style="background:${_rmsShowerColor(s)}" aria-hidden="true"></i><span class="sa-mono">${escHtml(s)}</span><span class="dt-n">${counts[s]}</span>`;
    return `<button type="button" class="rms-flt-btn${on ? ' active' : ''}" aria-pressed="${on}" data-shower="${escHtml(s)}"
             ${s === 'all' ? '' : `title="${escHtml(showerFullName(s))}"`}
             onclick="rmsSelectShower('${escHtml(host)}','${escHtml(cam)}',this.dataset.shower)">${label}</button>`;
  }).join('');
}

function rmsSelectShower(host, cam, shower) {
  const row = document.getElementById(`rms-shower-btns-${host}-${cam}`);
  if (!row) return;
  const allBtn = row.querySelector('[data-shower="all"]');
  if (shower === 'all') {
    // Clear all specific picks and activate "All" exclusively
    row.querySelectorAll('.rms-flt-btn').forEach(b => b.classList.remove('active'));
    if (allBtn) allBtn.classList.add('active');
  } else {
    // Matched by value, not a [data-shower="..."] selector a quote would break.
    const btn = [...row.querySelectorAll('.rms-flt-btn')].find(b => b.dataset.shower === shower);
    if (!btn) return;
    btn.classList.toggle('active');
    if (allBtn) allBtn.classList.remove('active');
    // If the user toggled the last specific pick off, fall back to "All"
    if (!row.querySelector('.rms-flt-btn.active') && allBtn) {
      allBtn.classList.add('active');
    }
  }
  row.querySelectorAll('.rms-flt-btn').forEach(b => b.setAttribute('aria-pressed', String(b.classList.contains('active'))));
  rmsApplyFilter(host, cam);
}

function rmsClearFilter(host, cam) {
  rmsSelectShower(host, cam, 'all');
  const sortEl = document.getElementById(`rms-sort-${host}-${cam}`);
  if (sortEl) sortEl.value = 'date-desc';
  _rmsCalSelected = new Set();
  _rmsCalUpdateLabel(host, cam);
  rmsApplyFilter(host, cam);
}

function rmsApplyFilter(host, cam) {
  if (!_rmsResults.length) return;
  renderRMSAllNights(host, cam, _rmsResults);
}

function _rmsRenderCardHtml(host, cam, c, date, idx, opts) {
  const isManual = c.lock_type === 'manual';
  const isArchive = c.source === 'archive';
  const archiveImgSrc = isArchive && c.stack
    ? `/api/archive/file/${cam}/${date}/${c.stack_subdir || 'meteors'}/${encodeURIComponent(c.stack)}`
    : '';
  const thumbUrl = c.stack
    ? (archiveImgSrc || `/stack/${host}/${cam}/${date}/${encodeURIComponent(c.stack)}`)
    : null;
  const det = _lookupDetection(c.filename, c.meteor_time);
  const mag = det?.mag_apparent;
  const speed = det?.trajectory ? entrySpeedText(det.trajectory) : '';
  const orbit = det?.trajectory
    ? `<span class="dt-badge" title="Multi-station GMN trajectory${speed ? '; entry speed' : ''}">Orbit${speed ? `, ${speed}` : ''}</span>` : '';
  const manual = isManual ? '<span class="dt-badge">Locked by hand</span>' : '';
  const hidden = !!(c.hidden || det?.hidden);
  const hideBtn = hideTarget(det, cam) || hideTarget(c, cam)
    ? hideButtonHtml({ hidden }, `onclick="event.stopPropagation();rmsHideCard(${idx})"`) : '';
  return `<div class="vdb-chunk-card dt-card${hidden ? ' hd-hidden' : ''}" role="button" tabindex="0" onclick="rmsOpenModal(${idx})"
               aria-label="Meteor at ${escHtml(c.time)} UTC${mag != null ? `, ${mag.toFixed(1)} mag` : ''}">
    <div class="dt-thumb">
      ${thumbUrl ? `<img src="${thumbUrl}" loading="lazy" decoding="async" alt="" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">` : ''}
      <div class="vdb-chunk-nostack" style="${thumbUrl ? 'display:none' : ''}">&#9654;</div>
      <span class="dt-badges">${orbit}${manual}</span>
    </div>
    <div class="dt-meta">
      <span class="sa-mono dt-time">${escHtml(c.time)}</span>
      ${mag != null ? `<span class="sa-mono${mag < 0 ? ' dt-bright' : ''}">${mag >= 0 ? '+' : ''}${mag.toFixed(1)}</span>` : ''}
      ${det?.shower ? `<span class="sa-mono" style="color:${_rmsShowerColor(det.shower)}" title="${escHtml(showerFullName(det.shower))}">${escHtml(det.shower)}</span>` : ''}
      ${opts && opts.showDate ? `<span class="dt-date">${fmtDate(date)}</span>` : ''}
      <span class="sa-mono dt-dur">${det?.duration_s != null ? det.duration_s.toFixed(1) + ' s' : ''}</span>
      ${hidden ? hiddenNoteHtml(c.hidden ? c : det) : ''}
      ${hideBtn}
      ${thumbUrl ? `<button type="button" class="dt-icon" aria-label="Open the stack" title="Stack"
          onclick="event.stopPropagation();rmsOpenModal(${idx},true)"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l9 5-9 5-9-5 9-5z"/><path d="M3 13l9 5 9-5"/></svg></button>` : ''}
    </div>
  </div>`;
}

/* ── The night strip: every RMS detection of a night at its time, taller for
   brighter, coloured by shower; a meteor with a clip opens it. ── */

// Minutes after noon (the twilight slider's scale) of a detection's UTC time.
function _rmsDetMin(det) {
  // time_utc carries no zone ("2026-10-15T22:51:45"); bare, Date reads it as local time.
  const t = new Date(`${det.time_utc.slice(0, 19).replace(' ', 'T')}Z`);
  return window.twilightSlider.utcMinToSliderVal(t.getUTCHours() * 60 + t.getUTCMinutes()) + t.getUTCSeconds() / 60;
}

// The strip's span for this camera: its detections' earliest and latest
// time, padded to the hour; 16:00-05:00 UTC when there are none yet.
export function rmsStripDomain(mins) {
  if (!mins.length) return { lo: 240, hi: 1020 };
  const lo = Math.max(0, Math.floor((Math.min(...mins) - 30) / 60) * 60);
  const hi = Math.min(1440, Math.ceil((Math.max(...mins) + 30) / 60) * 60);
  return { lo, hi: Math.max(hi, lo + 120) };
}

// A night's numbers: meteors, brightest, busiest UTC hour, multi-station.
export function rmsNightStats(dets) {
  const mags = dets.map(d => d.mag_apparent).filter(m => m != null);
  const perHour = {};
  for (const d of dets) {
    const h = d.time_utc.slice(11, 13);
    perHour[h] = (perHour[h] || 0) + 1;
  }
  const peak = Object.entries(perHour).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0];
  return {
    meteors: dets.length,
    brightest: mags.length ? Math.min(...mags) : null,
    peakHour: peak ? peak[0] : null, peakCount: peak ? peak[1] : 0,
    orbits: dets.filter(d => d.trajectory).length,
  };
}

function _rmsStripHtml(dets, dom, clipIdx, shown, tall) {
  const span = dom.hi - dom.lo;
  const ticks = dets.map(d => {
    const x = (_rmsDetMin(d) - dom.lo) / span * 100;
    if (x < 0 || x > 100) return '';
    const mag = d.mag_apparent ?? 3;
    const h = Math.round(Math.max(0.15, Math.min(1, (4 - mag) / 6)) * (tall ? 52 : 22));
    const col = _rmsShowerColor(d.shower);
    const label = `${d.time_utc.slice(11, 19)} UTC${d.mag_apparent != null ? `, ${mag >= 0 ? '+' : ''}${mag.toFixed(1)} mag` : ''}${d.shower ? ', ' + showerFullName(d.shower) : ''}`;
    const style = `left:${x.toFixed(2)}%;height:${h}px;background:${col}${mag < 0 ? `;box-shadow:0 0 6px ${col}` : ''}${shown(d) ? '' : ';opacity:.25'}`;
    const idx = clipIdx.get(utcSecondKey(d.time_utc));
    return idx != null
      ? `<button type="button" class="dt-tick" style="${style}" title="${escHtml(label)}" aria-label="Play the meteor at ${escHtml(label)}" onclick="rmsOpenModal(${idx})"></button>`
      : `<span class="dt-tick" style="${style}" title="${escHtml(label)}"></span>`;
  }).join('');
  return `<div class="dt-strip${tall ? ' tall' : ''}" role="img" aria-label="${dets.length} meteors across the night">${ticks}</div>`;
}

function _rmsAxisHtml(dom) {
  const out = [];
  for (let m = Math.ceil(dom.lo / 120) * 120; m <= dom.hi; m += 120) {
    out.push(`<span class="sa-mono" style="left:${((m - dom.lo) / (dom.hi - dom.lo) * 100).toFixed(2)}%">${window.twilightSlider.sliderValToHHMM(m)}</span>`);
  }
  return `<div class="dt-axis" aria-hidden="true">${out.join('')}</div>`;
}

// Paint the strips' sky from one night's twilight (close enough for a week).
function _rmsPaintSky(host, cam) {
  const area = document.getElementById(`rms-chunks-${host}-${cam}`);
  if (area && _rmsTwilight && area._dom) window.twilightSlider.drawTwilightOnWrap(area, _rmsTwilight, area._dom);
}

function rmsExpandNight(host, cam, date) {
  _rmsExpanded.add(date);
  renderRMSAllNights(host, cam, _rmsResults);
  document.getElementById(`rms-night-sec-${host}-${cam}-${date}`)?.querySelector('.dt-card')?.focus();
}

/** Rollover-aware sort key for a {date, chunk} row. A night runs ~18:00→05:00
 *  UTC, so morning clips (hour < 12) belong AFTER the same night's evening
 *  clips. Mirror VDB's _allCamsTimeSec: add 86400s when the hour is < 12. */
function _rmsNightSortKey(row) {
  const t = row.chunk.time || '';
  const [h, m, s] = t.split(':').map(Number);
  const sec = (h || 0) * 3600 + (m || 0) * 60 + (s || 0);
  const rollover = h < 12 ? sec + 86400 : sec;
  return Number(row.date) * 172800 + rollover;
}

function renderRMSAllNights(host, cam, results) {
  const el = document.getElementById(`rms-chunks-${host}-${cam}`);
  if (!el) return;
  const zoom = document.getElementById(`rms-zoom-${host}-${cam}`)?.value || 220;
  const flt = _rmsReadFilter(host, cam);
  const summaryEl = document.getElementById(`rms-flt-summary-${host}-${cam}`);
  const shown = det => flt.shower.all || flt.shower.set.has(det?.shower);
  const showHidden = showingHidden();
  const listedDets = date => (_rmsNightDets.get(date) || []).filter(d => showHidden || !d.hidden);

  // Flatten with date annotation; apply filter; collect totals.
  let flat = [];
  let total = 0;
  for (const night of results) {
    if (flt.nights.size && !flt.nights.has(night.date)) {
      // Still count toward total so the summary reflects the filter ratio
      total += night.chunks.filter(c => showHidden || !(c.hidden || _lookupDetection(c.filename, c.meteor_time)?.hidden)).length;
      continue;
    }
    for (const c of night.chunks) {
      const det = _lookupDetection(c.filename, c.meteor_time);
      if (!showHidden && (c.hidden || det?.hidden)) continue;
      total++;
      if (!flt.shower.all && (!det || !flt.shower.set.has(det.shower))) continue;
      flat.push({ chunk: c, date: night.date, det });
    }
  }

  // Sort
  if (flt.sort === 'mag-asc' || flt.sort === 'mag-desc') {
    flat.sort((a, b) => {
      const am = a.det?.mag_apparent;
      const bm = b.det?.mag_apparent;
      if (am == null && bm == null) return 0;
      if (am == null) return 1;   // unknown mag → end
      if (bm == null) return -1;
      return flt.sort === 'mag-asc' ? am - bm : bm - am;
    });
  } else if (flt.sort === 'date-asc') {
    flat.sort((a, b) => _rmsNightSortKey(a) - _rmsNightSortKey(b));
  } else {
    // date-desc — newest first per night, nights newest first
    flat.sort((a, b) => _rmsNightSortKey(b) - _rmsNightSortKey(a));
  }

  // Build _rmsVisibleChunks for modal nav (annotate with _date for rmsOpenModal).
  _rmsVisibleChunks = flat.map(({ chunk, date }) => ({ ...chunk, _date: date }));
  // A strip tick opens its clip: detection second → index in the visible list
  const clipIdx = new Map();
  flat.forEach(({ chunk }, i) => { if (chunk.meteor_time) clipIdx.set(utcSecondKey(chunk.meteor_time), i); });

  // Filter summary
  if (summaryEl) {
    summaryEl.textContent = (!flt.shower.all || flt.nights.size || flat.length !== total)
      ? `Showing ${flat.length} of ${total}.` : '';
  }

  if (!flat.length) {
    el.innerHTML = '<div class="no-data" style="padding:8px 0">No clips match the current filter</div>';
    return;
  }

  const grid = cards => `<div class="vdb-chunk-grid dt-grid" style="grid-template-columns:repeat(auto-fill,minmax(min(${zoom}px,100%),1fr))">${cards}</div>`;
  let html = '';
  if (flt.sort.startsWith('date-')) {
    const byDate = new Map();
    flat.forEach((row, idx) => {
      if (!byDate.has(row.date)) byDate.set(row.date, []);
      byDate.get(row.date).push({ row, idx });
    });
    const dates = [...byDate.keys()];
    const dom = rmsStripDomain(dates.flatMap(d => listedDets(d).map(_rmsDetMin)));
    el._dom = dom;
    dates.forEach((date, nightIdx) => {
      const entries = byDate.get(date);
      const dets = listedDets(date);
      const st = rmsNightStats(dets);
      // The first night is open; others open on request. A calendar pick
      // opens every picked night.
      const open = nightIdx === 0 || flt.nights.size > 0 || _rmsExpanded.has(date);
      const id = `rms-night-sec-${host}-${cam}-${date}`;
      const clipsN = `${entries.length} clip${entries.length !== 1 ? 's' : ''}`;
      if (!open) {
        html += `<section class="dt-night dt-night-closed" id="${id}" data-collapsed="1" aria-label="${nightTitle(date)}">
          <div class="sa-stack"><h3>${nightTitle(date)}</h3>
            <span class="sa-note">${st.meteors ? `<span class="sa-mono">${st.meteors}</span> meteors${st.brightest != null ? `, brightest <span class="sa-mono">${st.brightest >= 0 ? '+' : ''}${st.brightest.toFixed(1)}</span>` : ''}` : clipsN}</span></div>
          ${_rmsStripHtml(dets, dom, clipIdx, shown, false)}
          <button type="button" class="sa-btn dt-tall" onclick="rmsExpandNight('${host}','${cam}','${date}')">Show ${clipsN}</button>
        </section>`;
        return;
      }
      const stat = (label, value, unit = '') => `<div><dt>${label}</dt><dd class="sa-mono">${value}${unit ? `<span>${unit}</span>` : ''}</dd></div>`;
      const stats = st.meteors ? `<dl class="dt-stats">
          ${stat('Meteors', st.meteors)}
          ${st.brightest != null ? stat('Brightest', `${st.brightest >= 0 ? '+' : ''}${st.brightest.toFixed(1)}`, ' mag') : ''}
          ${st.peakHour ? stat('Busiest hour', st.peakHour, `h UTC, ${st.peakCount} meteors`) : ''}
          ${st.orbits ? stat('Multi-station', st.orbits, ' with orbits') : ''}
        </dl>` : `<span class="sa-note">${clipsN}; no RMS detection data for this night</span>`;
      const cards = entries.map(({ row, idx }) => _rmsRenderCardHtml(host, cam, row.chunk, date, idx, { showDate: false })).join('');
      // The heading's id must not start with the section's prefix, which
      // _rmsNightInView matches to find the night in view.
      const hid = `rms-night-h-${host}-${cam}-${date}`;
      html += `<section class="dt-night" id="${id}" aria-labelledby="${hid}">
        <div class="dt-night-head"><h2 id="${hid}">${nightTitle(date)}</h2>${stats}</div>
        ${dets.length ? `<div class="dt-strip-wrap">${_rmsStripHtml(dets, dom, clipIdx, shown, true)}${_rmsAxisHtml(dom)}</div>` : ''}
        ${grid(cards)}
        <div id="rms-plots-${host}-${cam}-${date}"></div>
      </section>`;
    });
  } else {
    // Flat sorted view — one grid, date shown on each card
    el._dom = null;
    html = `<section class="dt-night">${grid(flat.map((row, idx) =>
      _rmsRenderCardHtml(host, cam, row.chunk, row.date, idx, { showDate: true })).join(''))}</section>`;
  }
  el.innerHTML = html;
  _rmsPaintSky(host, cam);
  // Plots for the open nights only: a closed night asks when it is opened.
  for (const sec of el.querySelectorAll('[id^="rms-plots-"]')) {
    const date = sec.id.slice(-8);
    const plots = _rmsPlots.get(`${host}|${cam}|${date}`);
    if (plots) renderRMSPlots(host, cam, date, plots, sec);
    else if (!_rmsPlotsAsked.has(date)) {
      _rmsPlotsAsked.add(date);
      fetchRMSPlots(host, cam, date, sec.id);
    }
  }
}

/** Hide state changed for the clip `filename` of night `date` (from a card
 *  or the video modal): mark the clip and its RMS detection with the server's
 *  answer and re-render, so a hidden card leaves the list (or turns dimmed
 *  when hidden detections are shown). */
function rmsOnHiddenChange(date, filename, resp) {
  const night = _rmsResults.find(r => r.date === date);
  const chunk = night?.chunks.find(c => c.filename === filename);
  if (!chunk) return;
  applyHidden(chunk, resp);
  const det = _lookupDetection(chunk.filename, chunk.meteor_time);
  if (det) applyHidden(det, resp);
  const { station: host, selectedCamera: cam } = window.rmsState;
  if (host && cam) _rmsRenderKeepingPlace(host, cam, _rmsResults);
}

/** The Hide / Unhide button on card `idx` of the visible list. */
function rmsHideCard(idx) {
  const c = _rmsVisibleChunks[idx];
  if (!c) return;
  const cam = window.rmsState.selectedCamera;
  const det = _lookupDetection(c.filename, c.meteor_time);
  const target = hideTarget(det, cam) || hideTarget(c, cam);
  const hide = !(c.hidden || det?.hidden);
  toggleHide(target, hide, resp => rmsOnHiddenChange(c._date, c.filename, resp), { page: 'detections' });
}

function rmsOpenModal(idx, startInStack = false) {
  // Cycle through whatever the user is currently looking at: the filtered+
  // sorted list (built by renderRMSAllNights), falling back to the full
  // fetch order if filters haven't been applied yet.
  const list = _rmsVisibleChunks.length ? _rmsVisibleChunks : _rmsAllChunks;
  const c = list[idx];
  if (!c) return;
  const host = window.rmsState.station;
  const cam  = window.rmsState.selectedCamera;
  window._vdbVisibleChunks = list;
  window._modalOpenedFromRms = true;
  const detOff = c.source === 'archive' ? null : (c.lock_type === 'detection' ? (c.detection_offset_s ?? null) : null);
  const videoSrc = c.source === 'archive'
    ? `/api/archive/file/${cam}/${c._date}/meteors/${encodeURIComponent(c.filename)}`
    : '';
  window.vdbOpenModal(host, cam, c._date, c.filename, c.time, c.size_mb, detOff, idx, videoSrc, startInStack);
}

function rmsUpdateZoom(host, cam, val) {
  const area = document.getElementById(`rms-chunks-${host}-${cam}`);
  if (!area) return;
  area.querySelectorAll('.dt-grid').forEach(g => {
    g.style.gridTemplateColumns = `repeat(auto-fill,minmax(min(${val}px,100%),1fr))`;
  });
}

/* ─────────────────────────────────────────
   RMS Plots
───────────────────────────────────────── */
async function fetchRMSPlots(host, cam, date, containerId) {
  const id = containerId || `rms-plots-${host}-${cam}`;
  if (!document.getElementById(id)) return;
  try {
    const rmsApiHost = window.hostForCamera(host, cam);
    const r = await fetch(`/api/rms/plots/${rmsApiHost}/${cam}/${date}`);
    if (!r.ok) return;
    const plots = await r.json();
    _rmsPlots.set(`${host}|${cam}|${date}`, plots);
    // Look the container up again: the list may have been re-rendered while
    // the request ran (a later loading step, a filter change).
    const area = document.getElementById(id);
    if (area) renderRMSPlots(host, cam, date, plots, area);
  } catch(e) {
    /* no gallery for this night */
  }
}

/* The `rotate` flag on a camera describes its sensor orientation. Only
   raw sensor frames inherit it — FF stacks, max/avepixel. Matplotlib
   charts (radiants, fieldsums, calibration plots, FF intervals, observing
   periods) carry axis labels and must stay upright. The colour meteor
   stack is pre-rotated upstream by the station stacker, so it never
   qualifies either. */
function _rmsPlotNeedsRotation(host, cam, filename) {
  const meta = state.STATIONS_META && state.STATIONS_META[host];
  if (!meta) return false;
  const c = (meta.cameras || []).find(x => x.code === cam);
  if (!c || !c.rotate) return false;
  if (filename === '__color_meteor_stack__.webp') return false;
  return /_(stack|maxpixel|avepixel)\./i.test(filename);
}

/* Parse /api/rms/plot_image/<host>/<cam>/<date>/<filename> back into its
   components so the modal can decide whether to rotate. Returns null for
   anything that isn't a plot_image URL (e.g. inline SVG placeholders). */
export function _parseRmsPlotImageUrl(imgUrl) {
  try {
    const u = new URL(imgUrl, location.href);
    const parts = u.pathname.split('/');
    const i = parts.indexOf('plot_image');
    if (i < 0 || parts.length < i + 5) return null;
    return {
      host:     parts[i + 1],
      cam:      parts[i + 2],
      date:     parts[i + 3],
      filename: decodeURIComponent(parts[i + 4]),
    };
  } catch (e) { return null; }
}

function renderRMSPlots(host, cam, date, plots, container) {
  if (!plots.length) { container.innerHTML = ''; return; }
  const rmsApiHost = window.hostForCamera(host, cam);
  // FF-based plots come from the station in native sensor orientation.
  // Apply the .thumb-rotated CSS class when the camera is flagged for
  // rotation — except the colour meteor stack, which the station stacker
  // already orients correctly per its own rotate flag.
  const cards = plots.map(p => {
    const imgUrl = `/api/rms/plot_image/${rmsApiHost}/${cam}/${date}/${encodeURIComponent(p.filename)}`;
    const rotCls = _rmsPlotNeedsRotation(rmsApiHost, cam, p.filename) ? ' class="thumb-rotated"' : '';
    const labelE = escHtml(p.label);
    return `<div class="rms-plot-card" role="button" tabindex="0" onclick="rmsPlotExpand(this,'${escHtml(imgUrl)}','${labelE}')">
      <img${rotCls} src="${escHtml(imgUrl)}" loading="lazy" decoding="async" alt="${labelE}"
           onerror="this.closest('.rms-plot-card').style.display='none'">
      <div class="rms-plot-label">${labelE}</div>
    </div>`;
  }).join('');
  container.innerHTML = `<details class="rms-plots-section dt-plots">
    <summary><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>
      RMS processing plots <span class="sa-note">${plots.length}</span></summary>
    <div class="rms-plots-grid">${cards}</div>
  </details>`;
}

/* ── Plot zoom state ── */
window._pz = { scale: 1, tx: 0, ty: 0, dragging: false, sx: 0, sy: 0, stx: 0, sty: 0 };

function _pzLimits(img) {
  return {
    maxTx: Math.max(0, img.offsetWidth  * (window._pz.scale - 1) / 2),
    maxTy: Math.max(0, img.offsetHeight * (window._pz.scale - 1) / 2),
  };
}
function _pzRubber(val, limit) {
  if (Math.abs(val) <= limit) return val;
  return Math.sign(val) * (limit + (Math.abs(val) - limit) * 0.25);
}
function _pzClamp(tx, ty, img) {
  const { maxTx, maxTy } = _pzLimits(img);
  return {
    tx: Math.max(-maxTx, Math.min(maxTx, tx)),
    ty: Math.max(-maxTy, Math.min(maxTy, ty)),
  };
}
function _pzApply(img, transition) {
  // When the modal is showing a rotated RMS plot, the .thumb-rotated class
  // adds rotate(180deg). Setting img.style.transform inline overrides the
  // class — append the rotation as the rightmost (= applied first) op so
  // the rotation happens before scale/translate. That keeps user pan/zoom
  // operating in screen coordinates rather than the rotated image frame.
  const rot = img.classList.contains('thumb-rotated') ? ' rotate(180deg)' : '';
  img.style.transition = transition || '';
  img.style.transform = `translate(${window._pz.tx}px,${window._pz.ty}px) scale(${window._pz.scale})${rot}`;
  img.style.transformOrigin = 'center';
  img.style.cursor = window._pz.scale > 1 ? (window._pz.dragging ? 'grabbing' : 'grab') : 'zoom-in';
}
function _pzBounce(img) {
  const { tx, ty } = _pzClamp(window._pz.tx, window._pz.ty, img);
  if (tx !== window._pz.tx || ty !== window._pz.ty) {
    window._pz.tx = tx; window._pz.ty = ty;
    _pzApply(img, 'transform 0.4s cubic-bezier(0.34,1.56,0.64,1)');
  }
}
function _pzWheel(e) {
  e.preventDefault();
  const img = document.getElementById('cam-modal-img');
  window._pz.scale = Math.max(1, Math.min(10, window._pz.scale * (e.deltaY < 0 ? 1.2 : 0.83)));
  if (window._pz.scale === 1) { window._pz.tx = 0; window._pz.ty = 0; }
  else { const c = _pzClamp(window._pz.tx, window._pz.ty, img); window._pz.tx = c.tx; window._pz.ty = c.ty; }
  _pzApply(img);
}
function _pzDown(e) {
  if (window._pz.scale <= 1) return;
  e.preventDefault();
  window._pz.dragging = true; window._pz.sx = e.clientX; window._pz.sy = e.clientY;
  window._pz.stx = window._pz.tx; window._pz.sty = window._pz.ty;
  const img = document.getElementById('cam-modal-img');
  img.style.transition = '';
  img.style.cursor = 'grabbing';
  document.addEventListener('mousemove', _pzMove);
  document.addEventListener('mouseup',   _pzUp);
}
function _pzMove(e) {
  if (!window._pz.dragging) return;
  const img = document.getElementById('cam-modal-img');
  const rawTx = window._pz.stx + e.clientX - window._pz.sx;
  const rawTy = window._pz.sty + e.clientY - window._pz.sy;
  const { maxTx, maxTy } = _pzLimits(img);
  window._pz.tx = _pzRubber(rawTx, maxTx);
  window._pz.ty = _pzRubber(rawTy, maxTy);
  _pzApply(img);
}
function _pzUp() {
  if (!window._pz.dragging) return;
  window._pz.dragging = false;
  window._pz.justDragged = true;
  setTimeout(() => { window._pz.justDragged = false; }, 50);
  document.removeEventListener('mousemove', _pzMove);
  document.removeEventListener('mouseup',   _pzUp);
  const img = document.getElementById('cam-modal-img');
  if (img) _pzBounce(img);
}

function rmsPlotExpand(card, imgUrl, label) {
  const modal = document.getElementById('cam-modal');
  const img   = document.getElementById('cam-modal-img');
  const title = document.getElementById('cam-modal-title');
  if (!modal || !img) return;
  title.textContent = label;
  img.src = imgUrl;
  const dlBtn = document.getElementById('cam-modal-dl');
  if (dlBtn) { dlBtn.href = imgUrl; dlBtn.style.display = ''; }
  // Mirror the per-card rotation policy onto the shared modal img. The
  // .thumb-rotated class is read by _pzApply when computing the inline
  // transform so zoom/pan compose correctly with the 180° flip.
  const info = _parseRmsPlotImageUrl(imgUrl);
  if (info && _rmsPlotNeedsRotation(info.host, info.cam, info.filename)) {
    img.classList.add('thumb-rotated');
  } else {
    img.classList.remove('thumb-rotated');
  }
  // Reset and enable zoom
  window._pz = { scale: 1, tx: 0, ty: 0, dragging: false, sx: 0, sy: 0, stx: 0, sty: 0 };
  img.style.transform = img.classList.contains('thumb-rotated') ? 'rotate(180deg)' : '';
  img.style.cursor = 'zoom-in';
  modal.dataset.plotZoom = '1';
  modal.classList.add('plot-mode');
  img.removeEventListener('wheel',     _pzWheel);
  img.removeEventListener('mousedown', _pzDown);
  img.addEventListener('wheel',     _pzWheel, { passive: false });
  img.addEventListener('mousedown', _pzDown);
  _modalOpen(modal);
}

// Expose to global scope for inline onclick handlers
window.rmsCalClearSelection = rmsCalClearSelection;
window.rmsCalGo = rmsCalGo;
window.rmsCalNav = rmsCalNav;
window.rmsCalToggle = rmsCalToggle;
window.rmsClearFilter = rmsClearFilter;
window.rmsOpenModal = rmsOpenModal;
window.rmsHideCard = rmsHideCard;
window.rmsOnHiddenChange = rmsOnHiddenChange;
window.rmsPlotExpand = rmsPlotExpand;
window.rmsSelectCamera = rmsSelectCamera;
window.rmsSelectShower = rmsSelectShower;
window.renderRMS = renderRMS;
window._lookupDetection = _lookupDetection;
window._pzDown = _pzDown;
window._pzMove = _pzMove;
window._pzUp = _pzUp;
window._pzWheel = _pzWheel;
window.fetchRMSAllNights = fetchRMSAllNights;
window.rmsCalDayDown = rmsCalDayDown;
window.rmsCalDayEnter = rmsCalDayEnter;
window.rmsApplyFilter = rmsApplyFilter;
window.rmsUpdateZoom = rmsUpdateZoom;
window.rmsExpandNight = rmsExpandNight;
