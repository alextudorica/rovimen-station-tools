import { openTimePicker, wireTimeField } from './dashboard-time-picker.js';
import { fmtBytes, fmtDate, parseFn, fetchJson, _modalOpen, _modalClose, _closeAllOpenModals, escHtml, state, _toast, LOCK_ICON, UNLOCK_ICON, canToggleLock, lockClip, buildTrimmedShortclipUrl, nearestNight, sameClip, noteUntrimmed, shortNightLabel, saveBlob, parseHHMM, CLIP_PRE_S, CLIP_POST_S } from './dashboard-common.js';
import { VideoModal } from './dashboard-video-modal.js';
import { MultiDetModal } from './dashboard-multi-det-modal.js';
import { hideTarget, toggleHide, hideButtonHtml, hiddenNoteText, applyHidden } from './dashboard-hidden.js';
/*
 * dashboard-vdb.js
 * ────────────────
 * Video Database tab — chunks, all-cams filmstrip, twilight density bar, video modal + trim slider, stitch with adjacent chunks, lock toggle, synced multi-cam modal. Loaded by dashboard.html before dashboard-station.js.
 *
 * Depends on dashboard-common.js (escHtml, fmtBytes, fmtDate, parseFn, state.STATIONS_META,
 * state.VDB_CAMERAS, state.statusData, state.vitalsData, state.tlData, state.activeStation, state.activeTab, state.IS_ADMIN,
 * state.AUTH_USER, state.USER_ROLE, state.USER_STATIONS, _modalOpen, _modalClose) and lexical bindings
 * declared at the top level of dashboard-station.js (classic non-module scripts share
 * the global lexical environment, so top-level `let`/`const` are visible across bundles).
 */

/* ─────────────────────────────────────────
   Video Database
───────────────────────────────────────── */
let vdbAllChunks       = [];
let _vdbReqSeq         = 0;    // monotonic guard: only the latest night/cam/filter request may commit vdbAllChunks + render
// Set as soon as the user moves a slider handle or types a time. The twilight
// clamp resolves asynchronously and would otherwise overwrite that range when
// it lands, which reads as "the time filter does nothing" — you set a window
// and it snaps back to sunset..sunrise a few seconds later. The window itself
// lives in state.view.from/to, so it survives camera, night, tab and station
// switches; only "Whole night" (or dragging back to the full range) hands the
// range back to the clamp.
let _vdbUserSetRange   = false;
// Badge sets and twilight markers decorate an already-rendered grid, so they
// get a bounded wait rather than blocking it. Long enough for a saturated
// HTTP/1.1 connection pool, short enough not to look stuck.
const VDB_DECORATION_TIMEOUT_MS = 4000;
window._vdbVisibleChunks  = [];   // ordered list shown in grid (for ←/→ nav)
let _vdbCurrentIdx     = -1;   // index of open video in window._vdbVisibleChunks
window._stackCurrentIdx   = -1;   // index of open stack in window._vdbVisibleChunks
window._stackModalCtx     = null; // {station, camera, date} for stack nav
window._vdbAzimuths    = {};   // camCode → az_centre degrees (window-scoped for cross-module access)
let vdbSliderTimer = null;
let vdbPollTimer   = null;
let vdbArchivedFiles = new Set();  // filenames in permanent archive
let vdbCachedFiles   = new Set();  // filenames cached on VPS (24h)
window.vdbSortNewest  = true;         // true = newest first (window-scoped for cross-module access)

window.vdbLoadedStation = null;
let _modalOpenedFromRms = false;  // true when the clip modal was opened from the RMS tab

// Shared VideoModal instance for the VDB clip viewer.
// Initialised lazily on first use so the container is guaranteed to exist in the DOM.
let _vdbModal = null;
function _getVdbModal() {
  if (!_vdbModal) {
    _vdbModal = new VideoModal(
      document.getElementById('vdb-modal-container'),
      { trim: true, stitch: true, nav: true }
    );
    if (!window._VideoModalCloseAll) {
      window._VideoModalCloseAll = () => VideoModal.closeAll();
    }
  }
  return _vdbModal;
}

// MultiDetModal instance for the All Cams synced viewer.
let _vdbSyncModal = null;
function _getVdbSyncModal() {
  if (!_vdbSyncModal) {
    _vdbSyncModal = new MultiDetModal(
      document.getElementById('vdb-sync-modal-container')
    );
  }
  return _vdbSyncModal;
}

/** Simple toast notification (no external dependency). */

// All-cams view mode (sessionStorage-scoped per host: 'single' | 'all_cams').
// In 'all_cams' the camera selector is hidden and we render a per-cam stack
// of chunk grids for the selected date; clicks open a synced multi-cam modal.
let vdbViewMode = 'single';
// Per-(host, cam, date) cache of chunk lists fetched for the all-cams view.
// Keyed `<host>|<cam>|<date>` → array of chunk records as returned by
// /api/videodb/chunks. Used both to render the per-cam grids and to find
// the ±300 s window neighbours when opening the synced modal.
let _vdbChunksByCam = {};

function vdbLoadViewMode(host) {
  try { return sessionStorage.getItem(`vdb_view_mode_${host}`) || 'single'; }
  catch (e) { return 'single'; }
}

function vdbSaveViewMode(host, mode) {
  try { sessionStorage.setItem(`vdb_view_mode_${host}`, mode); }
  catch (e) { /* ignore */ }
}

function vdbLoadAllCamsSort(host) {
  try { return sessionStorage.getItem(`vdb_allcams_sort_${host}`) !== 'asc'; }
  catch (e) { return true; }
}

/** Resolve the backend host key for a camera (handles merged stations). */
function hostForCamera(displayHost, camCode) {
  const meta = state.STATIONS_META[displayHost];
  if (!meta) return displayHost;
  const cam = meta.cameras.find(c => c.code === camCode);
  return (cam && cam._host) || displayHost;
}

/** Read #vdb-night <select> and return compact YYYYMMDD. */
function vdbGetNight() {
  return document.getElementById('vdb-night').value || '';
}

/** Set #vdb-night <select> value from compact YYYYMMDD. */
function vdbSetNight(yyyymmdd) {
  document.getElementById('vdb-night').value = yyyymmdd || '';
}

function vdbIsTonight() {
  const now  = new Date();
  const utcH = now.getUTCHours();
  const base = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(),
                                  now.getUTCDate() + (utcH >= 12 ? 0 : -1)));
  const tonight = `${base.getUTCFullYear()}${String(base.getUTCMonth()+1).padStart(2,'0')}${String(base.getUTCDate()).padStart(2,'0')}`;
  return vdbGetNight() === tonight;
}

function vdbStopPoll() {
  if (vdbPollTimer) { clearInterval(vdbPollTimer); vdbPollTimer = null; }
  const badge = document.getElementById('vdb-live-badge');
  if (badge) badge.style.display = 'none';
}

function vdbStartPoll() {
  vdbStopPoll();
  const isLive = vdbIsTonight();
  const badge = document.getElementById('vdb-live-badge');
  if (badge) badge.style.display = isLive ? 'inline' : 'none';
  // 60 s for live (was 30 s — chunks land every few minutes at most, faster
  // polling just wastes the ~300 KB chunks payload), 120 s for historical
  // (was 60 s — backed by ETag/304 anyway, but no need to wake up the tab).
  vdbPollTimer = setInterval(vdbPoll, isLive ? 60000 : 120000);
}

async function vdbPoll() {
  if (document.hidden) return;
  const station = state.activeStation;
  const camera  = vdbGetSelectedCamera();
  const date    = vdbGetNight();
  if (!station || !camera || !date) return;
  try {
    const pollHost = hostForCamera(station, camera);
    const [chunksResp, cacheResp] = await Promise.all([
      fetch(`/api/videodb/chunks/${pollHost}/${camera}/${date}?locked_only=0`),
      fetch(`/api/cached-files/${camera}/${date}`).catch(() => ({ok:false})),
    ]);
    if (!chunksResp.ok) {
      if (chunksResp.status === 401) {
        _toast('Session expired, please reload', 'error');
        vdbStopPoll();
      } else if (chunksResp.status === 403) {
        _toast('You do not have access to this station', 'error');
        vdbStopPoll();
      }
      return;
    }
    const fresh = await chunksResp.json();
    let changed = fresh.length !== vdbAllChunks.length;
    if (cacheResp.ok) {
      const cached = new Set(await cacheResp.json());
      if (cached.size !== vdbCachedFiles.size) { vdbCachedFiles = cached; changed = true; }
    }
    if (changed) {
      vdbAllChunks = fresh;
      vdbRedrawCanvas();
      vdbRenderResults();
    }
  } catch(e) { /* ignore poll errors silently */ }
}

export function _vdbAzCompass(deg) {
  const dirs = ['N','NE','E','SE','S','SW','W','NW'];
  return dirs[Math.round(((deg % 360) + 360) % 360 / 45) % 8];
}

async function vdbLoadAzimuths(host) {
  try {
    const r = await window.fetchOnce(`tab:platepar:${host}`, `/api/platepar/${host}`);
    if (!r.ok) return;
    const pp = await r.json();
    window._vdbAzimuths = {};
    for (const [cam, d] of Object.entries(pp)) {
      if (d && d.az_centre != null) window._vdbAzimuths[cam] = d.az_centre;
    }
    vdbUpdateAzLabels();
    if (host === state.activeStation) window.renderTimelapses(host);
  } catch(e) {
    if (window._isAbort?.(e)) return;
  }
}

function vdbUpdateAzLabels() {
  const cam = vdbGetSelectedCamera();
  const lbl = document.getElementById('vdb-cam-pointing');
  if (!lbl) return;
  const az = window._vdbAzimuths[cam];
  lbl.textContent = az != null ? `${_vdbAzCompass(az)} · ${Math.round(az)}°` : '';
}

function vdbInit() {
  window.vdbLoadedStation = state.activeStation;
  const host = state.activeStation;
  const vdbHeader = document.querySelector('.vdb-sticky-header');

  // Ensure a banner placeholder exists just before the VDB sticky header
  let banner = document.getElementById('vdb-access-banner');
  if (!banner && vdbHeader) {
    banner = document.createElement('div');
    banner.id = 'vdb-access-banner';
    vdbHeader.parentNode.insertBefore(banner, vdbHeader);
  }

  if (window.canAccessTab('videodb', host)) {
    if (banner) banner.innerHTML = '';
    if (vdbHeader) vdbHeader.style.display = '';
    vdbViewMode = vdbLoadViewMode(host);
    window.vdbAllCamsSortDesc = vdbLoadAllCamsSort(host);
    // Mirror the per-host sort onto the single-cam state so flipping the
    // direction in either view stays consistent within a host (P1-34).
    window.vdbSortNewest = window.vdbAllCamsSortDesc;
    const _sortBtn0 = document.getElementById('vdb-sort-btn');
    if (_sortBtn0) _sortBtn0.innerHTML = vdbSortLabel(window.vdbSortNewest);
    vdbEnsureViewToggle(host);
    vdbApplyViewModeUI();
    vdbPopulateCameras();
    vdbOnCameraChange();
  } else {
    if (banner) banner.innerHTML = window._accessBanner('Video Database');
    if (vdbHeader) vdbHeader.style.display = 'none';
  }
}

/** Inject the Single / All cams segmented toggle into the sticky header
 *  (idempotent — re-running just rewires onclick / active classes). */
function vdbEnsureViewToggle(host) {
  const filterRight = document.querySelector('#pane-videodb .vdb-filter-right');
  if (!filterRight) return;
  let toggle = document.getElementById('vdb-view-toggle');
  if (!toggle) {
    toggle = document.createElement('div');
    toggle.id = 'vdb-view-toggle';
    toggle.className = 'vdb-view-toggle';
    // Prepend so it sits visually before night-select + locked-only on the
    // right-hand cluster (matches the order: view-mode → date → filters).
    filterRight.insertBefore(toggle, filterRight.firstChild);
  }
  const isMobile = window.innerWidth < 768;
  toggle.innerHTML = `
    <button class="vdb-toggle-btn ${vdbViewMode === 'single' ? 'active' : ''}"
            onclick="vdbSwitchView('${host}','single')">Single cam</button>
    <button class="vdb-toggle-btn ${vdbViewMode === 'all_cams' ? 'active' : ''}"
            onclick="vdbSwitchView('${host}','all_cams')"
            style="${isMobile ? 'display:none' : ''}">All cams</button>`;

  // All-cams chunk-order toggle (only meaningful + visible in all-cams mode).
  // Lives as a sibling to the view toggle so they cluster visually.
  let sort = document.getElementById('vdb-allcams-sort');
  if (!sort) {
    sort = document.createElement('div');
    sort.id = 'vdb-allcams-sort';
    sort.className = 'vdb-view-toggle';
    sort.style.marginLeft = '6px';
    toggle.insertAdjacentElement('afterend', sort);
  }
  sort.innerHTML = `
    <button class="vdb-toggle-btn ${window.vdbAllCamsSortDesc ? 'active' : ''}"
            title="Newest clips first" onclick="vdbSetAllCamsSort('${host}',true)">Newest first</button>
    <button class="vdb-toggle-btn ${!window.vdbAllCamsSortDesc ? 'active' : ''}"
            title="Oldest clips first" onclick="vdbSetAllCamsSort('${host}',false)">Oldest first</button>`;
  sort.style.display = vdbViewMode === 'all_cams' ? '' : 'none';
}

/** Persist + apply the All-cams chunk sort direction. Re-renders each
 *  per-camera filmstrip from its cached chunk list (no re-fetch). */
function vdbSetAllCamsSort(host, desc) {
  if (window.vdbAllCamsSortDesc === desc) return;
  window.vdbAllCamsSortDesc = desc;
  // Mirror onto the single-cam sort state + the legacy "Newest first" pill
  // header so the two views never disagree (P1-34).
  window.vdbSortNewest = desc;
  const singleBtn = document.getElementById('vdb-sort-btn');
  if (singleBtn) singleBtn.innerHTML = vdbSortLabel(window.vdbSortNewest);
  try { sessionStorage.setItem(`vdb_allcams_sort_${host}`, desc ? 'desc' : 'asc'); } catch (e) { /* ignore */ }
  vdbEnsureViewToggle(host);
  if (vdbViewMode !== 'all_cams') return;
  const date = vdbGetNight();
  if (!date) return;
  const cams = state.VDB_CAMERAS[host] || [];
  _vdbAllCamsRenderTimeline(host, cams, date);
}

/** Show / hide the per-cam selector + sort + locked-only based on view mode.
 *  The shared date picker stays visible in either mode. */
function vdbApplyViewModeUI() {
  const camSel    = document.getElementById('vdb-camera');
  const camPtg    = document.getElementById('vdb-cam-pointing');
  const lockedLbl = document.getElementById('vdb-locked-only')?.closest('.vdb-toggle-label');
  const sortBtn   = document.getElementById('vdb-sort-btn');
  const allCams   = vdbViewMode === 'all_cams';
  // The camera selector and its pointing label only matter in single-cam mode.
  if (camSel) camSel.style.display = allCams ? 'none' : '';
  if (camPtg) camPtg.style.display = allCams ? 'none' : '';
  // "Manual locks only" and the sort button operate against vdbAllChunks for the
  // single active camera. In all-cams mode we render per-cam grids straight
  // from the raw chunk lists, so hide them to avoid stale UI controls.
  if (lockedLbl) lockedLbl.style.display = allCams ? 'none' : '';
  if (sortBtn)   sortBtn.style.display   = allCams ? 'none' : '';
  // The cam-label on the camera filter row would otherwise reserve space
  // when the buttons are hidden — hide its <label> too.
  const camLabel = document.querySelector('#pane-videodb .vdb-filter-group');
  if (camLabel) camLabel.style.display = allCams ? 'none' : '';
}

function vdbSwitchView(host, mode) {
  if (vdbViewMode === mode) return;
  vdbViewMode = mode;
  vdbSaveViewMode(host, mode);
  vdbEnsureViewToggle(host);
  vdbApplyViewModeUI();
  // Stop the single-cam poll when leaving single mode — it polls one camera
  // anyway and would just churn against the now-hidden selector.
  vdbStopPoll();
  // Re-fetch and re-render under the new mode using the current date.
  if (mode === 'all_cams') {
    vdbRenderAllCams(host);
  } else {
    // Going back to single-cam: clear the all-cams DOM and re-run the
    // normal single-camera fetch path so polling resumes.
    document.getElementById('vdb-results').innerHTML = '';
    vdbOnCameraChange();
  }
}

function vdbGetSelectedCamera() {
  const container = document.getElementById('vdb-camera');
  if (!container) return null;
  const sel = container.querySelector('select');
  if (sel) return sel.value;
  const active = container.querySelector('.cam-btn.active');
  return active ? active.dataset.cam : null;
}

function vdbPopulateCameras() {
  const station = state.activeStation;
  const container = document.getElementById('vdb-camera');
  container.innerHTML = '';
  const cams = state.VDB_CAMERAS[station] || [];
  // The camera picked on another tab (or carried by the URL), else the first.
  const current = cams.includes(state.view.cam) ? state.view.cam : cams[0];
  if (window.innerWidth <= 540) {
    const sel = document.createElement('select');
    sel.setAttribute('aria-label', 'Camera');
    cams.forEach(c => {
      const opt = document.createElement('option');
      opt.value = c; opt.textContent = c;
      sel.appendChild(opt);
    });
    if (current) sel.value = current;
    sel.onchange = () => { vdbPickCamera(sel.value); };
    container.appendChild(sel);
  } else {
    cams.forEach(c => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'cam-btn' + (c === current ? ' active' : '');
      btn.setAttribute('aria-pressed', String(c === current));
      btn.dataset.cam = c;
      btn.textContent = c;
      btn.onclick = () => {
        container.querySelectorAll('.cam-btn').forEach(b => {
          b.classList.remove('active');
          b.setAttribute('aria-pressed', 'false');
        });
        btn.classList.add('active');
        btn.setAttribute('aria-pressed', 'true');
        vdbPickCamera(c);
      };
      container.appendChild(btn);
    });
  }
}

/** Camera pick by the user: pin it in state.view and the URL (as the night
 *  and window are), then load it. Without the URL write, Back from a clip
 *  rebuilt the Video DB on the camera the page had been opened with. */
function vdbPickCamera(cam) {
  state.view.cam = cam;
  window._pendingClip = null;   // the user moved on from a linked clip
  window.stationSyncUrl?.();
  vdbOnCameraChange();
}

async function vdbOnCameraChange() {
  const token = ++_vdbReqSeq;
  // Close any clip modal still open from the previous camera so the
  // new camera's results aren't hidden behind a stale player.
  _closeAllOpenModals();
  vdbStopPoll();
  const station  = state.activeStation;
  const camera   = vdbGetSelectedCamera();
  const apiHost  = hostForCamera(station, camera);
  const nightSel = document.getElementById('vdb-night');
  const statusEl = document.getElementById('vdb-status');
  nightSel.innerHTML = '';
  statusEl.textContent = 'Loading\u2026';
  vdbUpdateAzLabels();
  document.getElementById('vdb-results').innerHTML  = '';
  vdbAllChunks = [];
  if (!camera) { statusEl.textContent = ''; return; }
  try {
    const nights = await fetchJson(`/api/videodb/nights/${apiHost}/${camera}`);
    if (token !== _vdbReqSeq) return;
    if (!nights.length) {
      statusEl.textContent = 'No nights available';
      return;
    }
    // Populate dropdown — nights are newest-first
    const label = d => vdbShortNight(d);
    nightSel.innerHTML = nights.map(d => `<option value="${escHtml(d)}" data-short="${escHtml(vdbShortNight(d))}">${escHtml(label(d))}</option>`).join('');
    // Stay on the night the user is looking at (the closest one this camera
    // has) instead of jumping back to the newest.
    const pinned = nearestNight(nights, state.view.date);
    if (pinned) {
      vdbSetNight(pinned);
      statusEl.textContent = '';
      vdbOnNightChange();
      return;
    }
    // Nothing pinned: pick the newest night that actually has chunks. The
    // /api/videodb/nights endpoint can return placeholder dirs (e.g. the upcoming night's stub
    // before sunset), so naively picking nights[0] strands the user on
    // "Loading..." / "no clips found" — see RO000H @ 2026-05-23
    // found in an audit.
    let chosen = nights[0];
    const candidates = nights.slice(0, 5);
    const probes = await Promise.allSettled(
      candidates.map(n =>
        fetchJson(`/api/videodb/chunks/${apiHost}/${camera}/${n}?locked_only=0`)
      )
    );
    if (token !== _vdbReqSeq) return;
    const firstGood = probes.findIndex(r => {
      if (r.status !== 'fulfilled') return false;
      const v = r.value;
      return Array.isArray(v) ? v.length > 0 : (v?.chunks?.length || 0) > 0;
    });
    if (firstGood >= 0) chosen = candidates[firstGood];
    vdbSetNight(chosen);
    statusEl.textContent = '';
    vdbOnNightChange();
  } catch(e) {
    statusEl.textContent = 'Error loading nights';
  }
}

export function vdbNextDay(nightDate) {
  const y = +nightDate.slice(0,4), m = +nightDate.slice(4,6)-1, d = +nightDate.slice(6,8);
  const dt = new Date(Date.UTC(y, m, d+1));
  return `${dt.getUTCFullYear()}-${String(dt.getUTCMonth()+1).padStart(2,'0')}-${String(dt.getUTCDate()).padStart(2,'0')}`;
}

async function vdbOnNightChange() {
  const token = ++_vdbReqSeq;
  const station  = state.activeStation;
  const camera   = vdbGetSelectedCamera();
  const apiHost  = hostForCamera(station, camera);
  const date     = vdbGetNight();
  const statusEl = document.getElementById('vdb-status');
  // Bailing here after ++_vdbReqSeq invalidates whichever request was in
  // flight, so leaving the status untouched strands a permanent "Loading…".
  if (!date) { statusEl.textContent = ''; return; }
  document.getElementById('vdb-midnight-label').textContent = vdbNextDay(date);
  // All-cams mode shares the date picker with single-cam but fans out chunk
  // fetches per-camera. Still kick off the twilight clamp so the shared
  // time-slider markers stay accurate; ignore single-cam-only side effects.
  if (vdbViewMode === 'all_cams') {
    statusEl.textContent = '';
    vdbAllChunks = [];
    vdbApplyViewRange();
    vdbRedrawCanvas();
    vdbClampToTwilight(station, date);
    vdbRenderAllCams(station);
    return;
  }
  statusEl.textContent = 'Loading\u2026';
  vdbAllChunks = [];
  vdbArchivedFiles = new Set();
  vdbCachedFiles = new Set();
  // The user's time window when they set one; else the full range while
  // fetching, which the twilight clamp below narrows.
  vdbApplyViewRange();
  vdbRedrawCanvas();
  try {
    // ONLY the chunk list gates the grid. Awaiting the badge sets and the
    // twilight clamp alongside it meant the slowest of four requests decided
    // when anything rendered — and until then vdbAllChunks stayed [], so the
    // time slider had nothing to filter. That is what presented as a stuck
    // "Loading…" next to a dead time filter: in prod the chunk list was ready
    // in 5 s but /api/archive/indexed took 11 s behind a saturated HTTP/1.1
    // connection pool, and it gated the whole panel.
    const chunks = await fetchJson(`/api/videodb/chunks/${apiHost}/${camera}/${date}?locked_only=0`);
    if (token !== _vdbReqSeq) return;
    vdbAllChunks = chunks;
    vdbRedrawCanvas();
    vdbRenderResults();
    vdbStartPoll();
    // Decoration, detached: each re-renders in place when it lands.
    vdbApplyDecorations(token, camera, date);
    vdbClampToTwilight(station, date, token);
  } catch(e) {
    statusEl.textContent = 'Error: ' + e.message;
  }
}

/** fetch() options carrying a bounded timeout, where the runtime supports it. */
function vdbTimeoutOpts(ms) {
  if (typeof AbortSignal !== 'undefined' && typeof AbortSignal.timeout === 'function') {
    return { signal: AbortSignal.timeout(ms) };
  }
  return {};
}

/**
 * Fetch the ARCHIVED / CACHED badge sets and re-render once they arrive.
 * Deliberately not awaited by the caller: a badge must never decide whether
 * the clip grid appears. Failures and timeouts degrade to "no badges".
 */
async function vdbApplyDecorations(token, camera, date) {
  const [archived, cached] = await Promise.all([
    fetch(`/api/archive/indexed/${camera}/${date}`, vdbTimeoutOpts(VDB_DECORATION_TIMEOUT_MS))
      .then(r => (r.ok ? r.json() : [])).catch(() => []),
    fetch(`/api/cached-files/${camera}/${date}`, vdbTimeoutOpts(VDB_DECORATION_TIMEOUT_MS))
      .then(r => (r.ok ? r.json() : [])).catch(() => []),
  ]);
  if (token !== _vdbReqSeq) return;
  vdbArchivedFiles = new Set(archived);
  vdbCachedFiles = new Set(cached);
  vdbRenderResults();
}

/**
 * The "Manual locks only" toggle is a pure client-side filter over the
 * already-fetched chunk list (the backend always serves the full night,
 * archived detections merged in), so flipping it just re-renders. No
 * re-fetch means the user-chosen time-range sliders survive untouched.
 */
function vdbOnLockedOnlyChange() {
  vdbRedrawCanvas();
  vdbRenderResults();
}

/** Sort button label: "↓ Newest first" / "↑ Oldest first". The caret span is
 *  hidden by the card's CSS; kept so the markup stays one shape. */
function vdbSortLabel(newest) {
  return newest
    ? '<span class="vdb-sort-ico" aria-hidden="true">\u2193 </span>Newest<span class="vdb-sort-more"> first</span><span class="vdb-sort-caret" aria-hidden="true"> &#9660;</span>'
    : '<span class="vdb-sort-ico" aria-hidden="true">\u2191 </span>Oldest<span class="vdb-sort-more"> first</span><span class="vdb-sort-caret" aria-hidden="true"> &#9650;</span>';
}

/** The night picker reads "20 Oct" (plus "· 78%" on the selected night once
 *  its moon is known); the full date is in each option's value. */
const vdbShortNight = shortNightLabel;

// ── Time picker on the two time fields (dashboard-time-picker.js) ──────────
// Desktop: a click selects the field for typing ("2333"), ↓ opens the wheels.
// Phones: the fields are read-only and a tap opens the wheel sheet.
let vdbLastTwilight = null;
// The station's footage window for the night (twilight-slider nightDomain:
// sun -9° → -9° + 30 min) in slider units. The 24 h bar stays (its chunk
// marks are drawn in 0-1440 units) but its thumbs, the time fields and the
// picker are held inside this span. Full day until the night's twilight lands.
let _vdbDomain = { lo: 0, hi: 1440 };
const _vdbClampVal = (v) => Math.min(_vdbDomain.hi, Math.max(_vdbDomain.lo, parseInt(v, 10)));

/** Sky colour of an hour (UTC): the shared helper the slider's colours come from. */
function vdbSkyBand(h) {
  return window.twilightSlider?.skyBandForHour(vdbLastTwilight, h) ?? null;
}

function vdbOpenTimePicker(which, extra = {}) {
  const sIn = document.getElementById('vdb-time-start');
  const eIn = document.getElementById('vdb-time-end');
  openTimePicker({
    ...extra,
    anchor: which === 'end' ? eIn : sIn,
    start: sIn.value, end: eIn.value, focus: which, skyBand: vdbSkyBand,
    limits: _vdbDomain.lo > 0 || _vdbDomain.hi < 1440 ? _vdbDomain : null,
    onSet: (s, e) => {
      sIn.value = s; vdbTimeInputChange('start');
      eIn.value = e; vdbTimeInputChange('end');
    },
  });
}

(function vdbWireTimePickers() {
  for (const [which, id] of [['start', 'vdb-time-start'], ['end', 'vdb-time-end']]) {
    const el = document.getElementById(id);
    if (el) wireTimeField(el, { which, openPicker: vdbOpenTimePicker });
  }
})();

function vdbDrawTwilightMarkers(tw) {
  // Renderer lives in twilight-slider.js (A-3: dedupe). Page-specific bits
  // (wrap selector, moon icon ID) are resolved by drawTwilightOnWrap.
  const wrap = document.querySelector('.vdb-slider-wrap');
  vdbLastTwilight = tw;
  window.twilightSlider.drawTwilightOnWrap(wrap, tw);
  const sel = document.getElementById('vdb-night');
  if (sel) {
    const pct = tw && tw.illumination != null ? Math.round(tw.illumination * 100) : null;
    for (const opt of sel.options) {
      if (!opt.dataset.short) continue;
      opt.textContent = opt.selected && pct != null ? `${opt.dataset.short} \u00b7 ${pct}%` : opt.dataset.short;
    }
  }
}

/**
 * Fetch twilight for a station+date and clamp VDB sliders to the station's
 * footage window (sun -9° → -9° + 30 min).
 *
 * `token` (optional) is the caller's _vdbReqSeq value: a clamp that resolves
 * after the user has moved on to another night/camera must not touch the
 * sliders. Callers that own no token (all-cams mode) omit it.
 */
async function vdbClampToTwilight(host, date, token) {
  const sEl = document.getElementById('vdb-slider-start');
  const eEl = document.getElementById('vdb-slider-end');
  if (!sEl || !eEl) return;
  try {
    const r = await fetch(`/api/twilight/${host}/${date}`,
                          vdbTimeoutOpts(VDB_DECORATION_TIMEOUT_MS));
    if (!r.ok) return;
    const tw = await r.json();
    if (token !== undefined && token !== _vdbReqSeq) return;
    // Markers describe the night itself, so they are always safe to draw.
    vdbDrawTwilightMarkers(tw);
    _vdbDomain = window.twilightSlider.nightDomain(tw);
    // Never clobber a range the user set while this was in flight (a late
    // clamp silently replacing a typed window is the other half of "the time
    // filter doesn't do anything"); only pull it into this night's window.
    if (_vdbUserSetRange) {
      sEl.value = _vdbClampVal(sEl.value);
      eEl.value = _vdbClampVal(eEl.value);
    } else {
      sEl.value = _vdbDomain.lo;
      eEl.value = _vdbDomain.hi;
    }
    vdbUpdateTimeLabel();
    // The grid rendered before this landed, so re-apply the filter to the
    // clamped range. All-cams mode owns its own renderer.
    if (vdbViewMode !== 'all_cams') {
      vdbRedrawCanvas();
      vdbRenderResults();
    }
  } catch(e) {
    // On failure, keep full 0..1440 range
  }
}

function vdbSliderInput(which) {
  const sEl = document.getElementById('vdb-slider-start');
  const eEl = document.getElementById('vdb-slider-end');
  // The range is now the user's; an in-flight twilight clamp must leave it
  // alone. vdbTimeInputChange delegates here, so typing a time takes
  // ownership too.
  _vdbUserSetRange = true;
  // No footage outside the station's capture window: the thumbs stop at it.
  sEl.value = _vdbClampVal(sEl.value);
  eEl.value = _vdbClampVal(eEl.value);
  if (which === 'start' && parseInt(sEl.value) > parseInt(eEl.value)) sEl.value = eEl.value;
  if (which === 'end'   && parseInt(eEl.value) < parseInt(sEl.value)) eEl.value = sEl.value;
  // Remember the window (and the night it was set on) so a camera, night,
  // tab or station switch keeps it. Dragging back to the full range (the
  // whole capture window) means "no window".
  const whole = parseInt(sEl.value) <= _vdbDomain.lo && parseInt(eEl.value) >= _vdbDomain.hi;
  Object.assign(state.view, whole ? { from: null, to: null } : vdbRangeToView(parseInt(sEl.value), parseInt(eEl.value)));
  if (state.view.from) state.view.date = vdbGetNight() || state.view.date;
  window.stationSyncUrl?.();
  vdbUpdateTimeLabel();
  vdbRedrawCanvas();
  clearTimeout(vdbSliderTimer);
  vdbSliderTimer = setTimeout(vdbRenderResults, 120);
}

/** Slider pair → state.view time window; the full range is no window. */
export function vdbRangeToView(startVal, endVal) {
  if (startVal <= 0 && endVal >= 1440) return { from: null, to: null };
  return { from: vdbSliderToFmt(startVal), to: vdbSliderToFmt(endVal) };
}

/** state.view time window → slider pair, or null when there is none. */
export function vdbViewToRange(view) {
  const s = view && view.from ? parseHHMM(view.from) : null;
  const e = view && view.to ? parseHHMM(view.to) : null;
  if (s == null || e == null) return null;
  return { startVal: vdbMinutesToSliderVal(s, false), endVal: vdbMinutesToSliderVal(e, true) };
}

/** Put the user's time window on the sliders, or the full range (for the
 *  twilight clamp to narrow) when they have not set one. */
function vdbApplyViewRange() {
  const sEl = document.getElementById('vdb-slider-start');
  const eEl = document.getElementById('vdb-slider-end');
  const r = vdbViewToRange(state.view);
  sEl.value = r ? _vdbClampVal(r.startVal) : _vdbDomain.lo;
  eEl.value = r ? _vdbClampVal(r.endVal) : _vdbDomain.hi;
  _vdbUserSetRange = !!r;
  vdbUpdateTimeLabel();
}

/** "Whole night": drop the user's window and re-clamp to twilight. */
function vdbResetTimeRange() {
  state.view.from = null;
  state.view.to = null;
  window.stationSyncUrl?.();
  vdbApplyViewRange();
  vdbRedrawCanvas();
  if (vdbViewMode !== 'all_cams') vdbRenderResults();
  const date = vdbGetNight();
  if (date) vdbClampToTwilight(state.activeStation, date, vdbViewMode === 'all_cams' ? undefined : _vdbReqSeq);
}

/** True when the loaded Video DB already shows state.view (its pinned
 *  camera, night and time window), so re-entering the tab can skip a reload. */
function vdbMatchesView() {
  const v = state.view;
  if (v.cam && v.cam !== vdbGetSelectedCamera()) return false;
  if (v.date && v.date !== vdbGetNight()) return false;
  const r = vdbViewToRange(v);
  const s = parseInt(document.getElementById('vdb-slider-start')?.value);
  const e = parseInt(document.getElementById('vdb-slider-end')?.value);
  if (r && (r.startVal !== s || r.endVal !== e)) return false;
  if (!r && _vdbUserSetRange) return false;
  return true;
}

/** Night <select> change by the user: pin the night, then load it. */
function vdbPickNight() {
  state.view.date = vdbGetNight() || null;
  window.stationSyncUrl?.();
  vdbOnNightChange();
}

export function vdbSliderToFmt(v) {
  const m = (v + 720) % 1440;
  return `${String(Math.floor(m/60)).padStart(2,'0')}:${String(m%60).padStart(2,'0')}`;
}

// minutes-of-day \u2192 slider value (slider is noon-anchored: 0 = noon, 720 = midnight, 1440 = next noon).
export function vdbMinutesToSliderVal(min, isEnd) {
  if (min === 720) return isEnd ? 1440 : 0;
  return ((min - 720) + 1440) % 1440;
}

function vdbUpdateTimeLabel() {
  const s = parseInt(document.getElementById('vdb-slider-start').value);
  const e = parseInt(document.getElementById('vdb-slider-end').value);
  const sIn = document.getElementById('vdb-time-start');
  const eIn = document.getElementById('vdb-time-end');
  if (sIn && document.activeElement !== sIn) { sIn.value = vdbSliderToFmt(s); sIn.classList.remove('invalid'); }
  if (eIn && document.activeElement !== eIn) { eIn.value = vdbSliderToFmt(e); eIn.classList.remove('invalid'); }
}

function vdbTimeInputChange(which) {
  const inEl = document.getElementById(which === 'start' ? 'vdb-time-start' : 'vdb-time-end');
  const slEl = document.getElementById(which === 'start' ? 'vdb-slider-start' : 'vdb-slider-end');
  const min = parseHHMM(inEl.value);
  if (min === null) { inEl.classList.add('invalid'); return; }
  inEl.classList.remove('invalid');
  slEl.value = vdbMinutesToSliderVal(min, which === 'end');
  vdbSliderInput(which);  // clamps + re-renders + writes back the formatted value
}

function vdbRedrawCanvas() {
  const canvas = document.getElementById('vdb-density-canvas');
  if (!canvas) return;
  const W = canvas.parentElement.clientWidth || 600;
  canvas.width = W;
  const H = 5;
  const ctx = canvas.getContext('2d');
  const s = parseInt(document.getElementById('vdb-slider-start').value);
  const e = parseInt(document.getElementById('vdb-slider-end').value);

  // All chunks — dim marks
  ctx.fillStyle = 'rgba(88,166,255,0.14)';
  const markW = Math.max(2, Math.ceil(W / 720));
  vdbAllChunks.forEach(c => {
    const p = c.time.split(':');
    const clockM = parseInt(p[0]) * 60 + parseInt(p[1]);
    const sliderPos = (clockM - 720 + 1440) % 1440;
    ctx.fillRect(Math.floor((sliderPos / 1440) * W), 0, markW, H);
  });

  // Selection range overlay
  ctx.fillStyle = 'rgba(88,166,255,0.12)';
  ctx.fillRect((s/1440)*W, 0, ((e-s)/1440)*W, H);

  // Filtered chunks — bright marks
  ctx.fillStyle = 'rgba(88,166,255,0.7)';
  vdbFilteredChunks().forEach(c => {
    const p = c.time.split(':');
    const clockM = parseInt(p[0]) * 60 + parseInt(p[1]);
    const sliderPos = (clockM - 720 + 1440) % 1440;
    ctx.fillRect(Math.floor((sliderPos / 1440) * W), 0, markW, H);
  });
}

/** Pure filter behind the "Manual locks only" toggle: keep only chunks a
 *  person locked (dashboard/API), dropping automatic detection locks and
 *  unlocked bulk footage. Exported for unit tests. */
export function vdbManualLockFilter(chunks, manualOnly) {
  if (!manualOnly) return chunks;
  return chunks.filter(c => c.lock_type === 'manual');
}

function vdbFilteredChunks() {
  const s = parseInt(document.getElementById('vdb-slider-start').value);
  const e = parseInt(document.getElementById('vdb-slider-end').value);
  const manualOnly = document.getElementById('vdb-locked-only')?.checked || false;
  const filtered = vdbManualLockFilter(vdbAllChunks, manualOnly).filter(c => {
    const p = c.time.split(':');
    const clockM = parseInt(p[0]) * 60 + parseInt(p[1]);
    const sliderPos = (clockM - 720 + 1440) % 1440;
    return sliderPos >= s && sliderPos <= e;
  });
  return window.vdbSortNewest ? filtered.slice().reverse() : filtered;
}

function vdbToggleSort() {
  window.vdbSortNewest = !window.vdbSortNewest;
  // Mirror onto the all-cams sort + persist per host (P1-34 — single & all-cams
  // share one sort direction).
  window.vdbAllCamsSortDesc = window.vdbSortNewest;
  try {
    if (state.activeStation) sessionStorage.setItem(
      `vdb_allcams_sort_${state.activeStation}`, window.vdbAllCamsSortDesc ? 'desc' : 'asc');
  } catch (e) { /* ignore */ }
  const btn = document.getElementById('vdb-sort-btn');
  if (btn) btn.innerHTML = vdbSortLabel(window.vdbSortNewest);
  // Refresh the all-cams sort segmented control so it matches.
  if (state.activeStation) vdbEnsureViewToggle(state.activeStation);
  vdbRenderResults();
}

/* Convert a (YYYYMMDD, HH:MM:SS) pair into epoch ms. The night folder name
   is the date the night STARTED; clips with HH < 12 belong to the following
   UTC calendar day (rollover at noon UTC — same convention as vdbIsTonight
   and the GMN matcher). Returns null on bad input. */
const VDB_STALE_MS = 5 * 60 * 1000;
export function vdbChunkUtcMs(ymd, hms) {
  if (!ymd || !hms || ymd.length < 8) return null;
  const [hh, mm, ss] = hms.split(':').map(n => parseInt(n, 10));
  if (Number.isNaN(hh) || Number.isNaN(mm)) return null;
  const y = parseInt(ymd.slice(0, 4), 10);
  const mo = parseInt(ymd.slice(4, 6), 10) - 1;
  let d = parseInt(ymd.slice(6, 8), 10);
  if (hh < 12) d += 1;
  return Date.UTC(y, mo, d, hh, mm, ss || 0);
}

/* Returns the index of the single visible clip we should paint stale, or
   -1 if none. A clip qualifies only when:
     1) it's the most-recent clip by time across the visible set, AND
     2) the selected night is tonight (RMS should still be live-capturing), AND
     3) it's more than VDB_STALE_MS behind wall-clock UTC.
   We deliberately don't paint every old clip red — that just means "old",
   not "the pipeline is lagging". */
function vdbStaleIdx(chunks, ymd) {
  if (!chunks.length || !vdbIsTonight()) return -1;
  let latestIdx = -1, latestMs = -Infinity;
  for (let i = 0; i < chunks.length; i++) {
    const cms = vdbChunkUtcMs(ymd, chunks[i].time);
    if (cms != null && cms > latestMs) { latestMs = cms; latestIdx = i; }
  }
  if (latestIdx < 0) return -1;
  return (Date.now() - latestMs) > VDB_STALE_MS ? latestIdx : -1;
}

/* Delegated click handler for the single-cam chunk grid. Reads all
   server-supplied values from data-* attributes (set via escHtml/JSON at
   render time) so no station-controlled string ever lands in an onclick
   attribute context. */
function _vdbGridClick(e) {
  const hideBtn = e.target.closest('.hd-btn');
  if (hideBtn) {
    e.stopPropagation();
    const card = hideBtn.closest('.vdb-chunk-card');
    if (!card) return;
    const d = JSON.parse(card.dataset.chunk || '{}');
    // The chunk's camera and meteor time name the detection (the server
    // resolves them, as for the Events cards).
    const chunk = vdbAllChunks.find(c => c.filename === d.filename);
    toggleHide(hideTarget(chunk, d.camera), hideBtn.dataset.hd === 'hide',
      resp => vdbOnHiddenChange(d.filename, resp), { page: 'video' });
    return;
  }
  const lockBtn = e.target.closest('.vdb-lock-trigger');
  if (lockBtn) {
    e.stopPropagation();
    const card = lockBtn.closest('.vdb-chunk-card');
    if (!card) return;
    const d = JSON.parse(card.dataset.chunk || '{}');
    vdbToggleLock(d.apiHost, d.camera, d.date, d.filename, lockBtn);
    return;
  }
  const processBtn = e.target.closest('.vdb-process-trigger');
  if (processBtn) {
    e.stopPropagation();
    const card = processBtn.closest('.vdb-chunk-card');
    if (!card) return;
    const d = JSON.parse(card.dataset.chunk || '{}');
    vdbProcessChunk(d.apiHost, d.camera, d.date, d.filename, processBtn);
    return;
  }
  const stackLink = e.target.closest('.vdb-stack-trigger');
  if (stackLink) {
    e.preventDefault();
    e.stopPropagation();
    const card = stackLink.closest('.vdb-chunk-card');
    if (!card) return;
    const d = JSON.parse(card.dataset.chunk || '{}');
    vdbOpenModal(d.apiHost, d.camera, d.date, d.filename, d.time, d.sizeMb, d.detOff, d.idx, d.archiveVideoSrc, true);
    return;
  }
  const card = e.target.closest('.vdb-card-trigger');
  if (card) {
    const d = JSON.parse(card.dataset.chunk || '{}');
    vdbOpenModal(d.apiHost, d.camera, d.date, d.filename, d.time, d.sizeMb, d.detOff, d.idx, d.archiveVideoSrc);
  }
}

function vdbRenderResults() {
  // No-op in all-cams mode — vdbRenderAllCams owns the #vdb-results DOM and
  // mutates per-camera grids directly; running the single-cam renderer here
  // would wipe them on every slider input.
  if (vdbViewMode === 'all_cams') return;
  const station   = state.activeStation;
  const camera    = vdbGetSelectedCamera();
  const apiHost   = hostForCamera(station, camera);
  const date      = vdbGetNight();
  const statusEl  = document.getElementById('vdb-status');
  const resultsEl = document.getElementById('vdb-results');
  const zoom      = document.getElementById('vdb-zoom').value;
  if (!date) return;
  const chunks = vdbFilteredChunks();
  window._vdbVisibleChunks = chunks;
  if (!chunks.length) {
    statusEl.textContent = vdbAllChunks.length
      ? `No clips in selected range \u00b7 ${vdbAllChunks.length} total for night`
      : 'No clips found.';
    resultsEl.innerHTML = '';
    return;
  }
  const _staleIdx = vdbStaleIdx(chunks, date);
  statusEl.innerHTML = `${chunks.length} clip${chunks.length!==1?'s':''}<span class="vdb-status-date"> \u00b7 ${escHtml(fmtDate(date))}</span>`;
  const cards = chunks.map((c, idx) => {
    const isArchive = c.source === 'archive';
    const thumbUrl = c.stack
      ? (isArchive
          ? `/api/archive/file/${camera}/${date}/${c.stack_subdir || 'meteors'}/${encodeURIComponent(c.stack)}`
          : `/stack/${apiHost}/${camera}/${date}/${encodeURIComponent(c.stack)}`)
      : null;
    const imgHtml = thumbUrl
      ? `<img src="${thumbUrl}" loading="lazy" decoding="async" alt="${c.time}"
             onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">`
      : '';
    const placeholderStyle = thumbUrl ? 'display:none' : '';
    const inArchive = vdbArchivedFiles.has(c.filename);
    const inCache = vdbCachedFiles.has(c.filename);
    const onServer = inArchive || inCache;
    const archiveClass = onServer ? ' vdb-chunk-archived' : '';
    const archiveBadge = inArchive ? '<span class="vdb-archived-badge">ARCHIVED</span>'
                       : inCache ? '<span class="vdb-archived-badge" style="background:rgba(88,166,255,.85)">CACHED</span>' : '';
    const lockClass = c.lock_type === 'manual' ? ' vdb-chunk-locked-manual'
                    : c.lock_type === 'detection' ? ' vdb-chunk-locked-detection' : '';
    const lockIcon  = c.locked ? LOCK_ICON : UNLOCK_ICON;
    // One-way lock (2026-07 hardening): the dashboard can only ADD a lock;
    // unlocking is SSH-only, so locked clips render a disabled button.
    const lockTitle = c.lock_type === 'detection' ? 'Detection locked'
                    : c.locked ? 'Locked — unlock via SSH only' : 'Lock this clip';
    const canLock = canToggleLock(apiHost);
    const lockBtn = c.lock_type === 'detection'
      ? `<button type="button" class="vdb-lock-btn vdb-lock-btn--detection" title="${lockTitle}" aria-label="${lockTitle}" disabled>${LOCK_ICON}</button>`
      : c.locked
      ? `<button class="vdb-lock-btn vdb-lock-btn--${c.lock_type || 'manual'}" title="${lockTitle}" aria-label="${lockTitle}" disabled>${LOCK_ICON}</button>`
      : canLock
      ? `<button type="button" class="vdb-lock-btn vdb-lock-trigger" title="${lockTitle}" aria-label="${lockTitle}"
         >${lockIcon}</button>`
      : '';
    // Real null (not the string 'null'): this rides inside the data-chunk JSON
    // payload and is JSON.parsed back in _vdbGridClick, so it must be a genuine
    // JS null. The old 'null' sentinel only worked when it was interpolated as a
    // JS literal into an inline onclick (removed for XSS hardening).
    const detOff   = c.lock_type === 'detection' ? (c.detection_offset_s ?? null) : null;
    const archiveVideoSrc = isArchive
      ? `/api/archive/file/${camera}/${date}/meteors/${encodeURIComponent(c.filename)}`
      : '';
    const archiveImgSrc = isArchive && c.stack
      ? `/api/archive/file/${camera}/${date}/${c.stack_subdir || 'meteors'}/${encodeURIComponent(c.stack)}`
      : '';
    const cardData = escHtml(JSON.stringify({
      apiHost, camera, date, filename: c.filename, time: c.time,
      sizeMb: c.size_mb, detOff, idx, archiveVideoSrc,
    }));
    const timeClass = idx === _staleIdx ? 'vdb-chunk-time vdb-time-stale' : 'vdb-chunk-time';
    // A detection clip can be hidden as a false positive. The Video DB lists
    // all footage, so a hidden one stays here, dimmed, with Unhide.
    const hideBtn = c.lock_type === 'detection' && c.meteor_time ? hideButtonHtml(c) : '';
    const hdNote = hiddenNoteText(c);
    return `<div class="vdb-chunk-card${lockClass}${archiveClass}${c.hidden ? ' hd-hidden' : ''} vdb-card-trigger" role="button" tabindex="0"
        data-filename="${escHtml(c.filename)}" data-lock-type="${escHtml(c.lock_type || '')}"
        ${hdNote ? `title="${escHtml(hdNote)}" ` : ''}data-chunk="${cardData}">
      ${archiveBadge}${lockBtn}
      ${imgHtml}
      <div class="vdb-chunk-nostack" style="${placeholderStyle}">\u25b6</div>
      <div class="vdb-chunk-meta">
        <div class="${timeClass}">${escHtml(c.time)} UTC</div>
        <div class="vdb-chunk-size">${c.size_mb != null ? c.size_mb + ' MB' : ''}${hideBtn}</div>
        <div class="vdb-chunk-btns">
          ${isArchive
            ? `<button class="vdb-process-btn" disabled title="Not available for archived clips">&#128190; Archive</button>`
            : c.reencoded
              ? `<button class="vdb-process-btn vdb-process-btn--done" disabled>&#10003; Processed</button>`
              : state.USER_ROLE === 'visitor'
              ? ''
              : `<button class="vdb-process-btn vdb-process-trigger" title="Encode with overlay &amp; colour calibration"
                   >&#9881; Process</button>`}
          ${thumbUrl ? `<a class="vdb-dl-btn vdb-stack-trigger" href="#">&#8718; Stack</a>` : ''}
        </div>
      </div>
    </div>`;
  }).join('');
  resultsEl.innerHTML = `<div class="card"><div class="vdb-chunk-grid" id="vdb-chunk-grid"
    style="grid-template-columns:repeat(auto-fill,minmax(min(${zoom}px,100%),1fr))">${cards}</div></div>`;
  // Attach delegated listeners so server-controlled strings never appear in
  // onclick attribute context. All payload comes from data-chunk (JSON, already
  // HTML-escaped at render time) rather than from inline event handlers.
  const grid = resultsEl.querySelector('#vdb-chunk-grid');
  if (grid) {
    grid.addEventListener('click', _vdbGridClick);
  }
  _vdbOpenPendingClip(grid);
}

/** ?clip=<filename>: open that clip's player the first time its card is on
 *  the grid (the grid re-renders as badges and the twilight clamp land, so
 *  a render without it just waits for the next). */
function _vdbOpenPendingClip(grid) {
  const want = window._pendingClip;
  if (!want || !grid) return;
  const card = [...grid.querySelectorAll('.vdb-card-trigger')]
    .find(c => sameClip(c.dataset.filename, want));
  if (!card) return;
  window._pendingClip = null;
  card.click();
}

function vdbUpdateZoom(val) {
  const tpl = `repeat(auto-fill,minmax(min(${val}px,100%),1fr))`;
  const grid = document.getElementById('vdb-chunk-grid');
  if (grid) grid.style.gridTemplateColumns = tpl;
  document.querySelectorAll('.vdb-cam-grid').forEach(g => {
    g.style.gridTemplateColumns = tpl;
  });
}

/* ─────────────────────────────────────────
   All-cams view: per-camera chunk grids
───────────────────────────────────────── */

let _vdbCurrentHost = null;

/** Parse a chunk time string (HH:MM:SS) to seconds, adjusted for night wraparound. */
function _allCamsTimeSec(t) {
  if (!t) return 0;
  const [h, m, s] = t.split(':').map(Number);
  const sec = h * 3600 + m * 60 + (s || 0);
  return h < 12 ? sec + 86400 : sec;
}

/** Group chunks from all cameras by timestamp proximity (±windowSec).
 *  Uses the first camera with data as the timeline reference. */
function _vdbGroupByTime(allChunks, cams, windowSec) {
  const refCam = cams.find(c => allChunks[c]?.length);
  if (!refCam) return [];
  return allChunks[refCam].map(refChunk => {
    const refSec = _allCamsTimeSec(refChunk.time);
    const slots = { [refCam]: refChunk };
    for (const cam of cams) {
      if (cam === refCam) continue;
      let best = null, bestDiff = Infinity;
      for (const c of (allChunks[cam] || [])) {
        const d = Math.abs(_allCamsTimeSec(c.time) - refSec);
        if (d < bestDiff) { bestDiff = d; best = c; }
      }
      if (best && bestDiff <= windowSec) slots[cam] = best;
    }
    return { refCam, refChunk, slots };
  });
}

/** Build one time-slot card showing all cameras side by side. */
function _vdbTimeSlotCardHtml(host, cams, date, group) {
  const camCells = cams.map(cam => {
    const chunk = group.slots[cam];
    if (!chunk) {
      return `<div class="vdb-ts-cam"><div class="vdb-ts-cam-lbl">${cam}</div><div class="vdb-ts-empty"></div></div>`;
    }
    const apiHost = hostForCamera(host, cam);
    const thumbUrl = chunk.stack ? `/stack/${apiHost}/${cam}/${date}/${encodeURIComponent(chunk.stack)}` : null;
    const imgHtml = thumbUrl
      ? `<img src="${thumbUrl}" loading="lazy" decoding="async" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">`
      : '';
    const phStyle = thumbUrl ? 'display:none' : '';
    const lockBadge = chunk.lock_type === 'detection'
      ? `<span class="vdb-ts-badge vdb-ts-badge--det" title="Detection locked">${LOCK_ICON}</span>`
      : chunk.locked
      ? `<span class="vdb-ts-badge vdb-ts-badge--manual" title="Manually locked">${LOCK_ICON}</span>`
      : '';
    return `<div class="vdb-ts-cam">
      <div class="vdb-ts-cam-lbl">${cam}</div>
      <div class="vdb-ts-thumb">
        ${imgHtml}
        <div class="vdb-chunk-nostack" style="${phStyle}">▶</div>
        ${lockBadge}
      </div>
    </div>`;
  }).join('');
  const { refChunk } = group;
  // Encode only the cameras that were actually matched (shown in this card).
  const slotMap = {};
  for (const [cam, chunk] of Object.entries(group.slots)) {
    slotMap[cam] = { f: chunk.filename, t: chunk.time, s: chunk.stack || null };
  }
  return `<div class="vdb-ts-card" data-host="${host}" data-date="${date}" data-slot="${escHtml(JSON.stringify(slotMap))}" role="button" tabindex="0" onclick="vdbOpenTimeSlot(this)">
    <div class="vdb-ts-cams">
      ${camCells}
      <button type="button" class="vdb-dome-btn" title="Sky dome view" aria-label="Sky dome view" onclick="event.stopPropagation();vdbOpenDome(this.closest('.vdb-ts-card'))">🌍</button>
    </div>
    <div class="vdb-ts-meta">
      <span>${refChunk.time}<br><span style="font-weight:400;opacity:.6">UTC</span></span>
    </div>
  </div>`;
}

/** Render the timeline from cached chunks. Safe to call multiple times. */
function _vdbAllCamsRenderTimeline(host, cams, date) {
  const grid = document.getElementById('vdb-allcam-global');
  if (!grid || grid.dataset.host !== host || grid.dataset.date !== date) return;
  const desc = !!window.vdbAllCamsSortDesc;
  const allChunks = {};
  for (const cam of cams) {
    const raw = _vdbChunksByCam[`${host}|${cam}|${date}`] || [];
    allChunks[cam] = raw.slice().sort((a, b) => {
      const cmp = _allCamsTimeSec(a.time) - _allCamsTimeSec(b.time);
      return desc ? -cmp : cmp;
    });
  }
  const groups = _vdbGroupByTime(allChunks, cams, 15);
  if (!groups.length) {
    grid.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:12px">No clips for this night</div>';
    return;
  }
  grid.innerHTML = groups.map(g => _vdbTimeSlotCardHtml(host, cams, date, g)).join('');
}

const _vdbAllCamsPending = new Map();

/** Fetch chunks for one camera, cache them, then render the timeline once all
 *  cameras for this host+date have loaded. */
async function vdbFetchAndRenderCamChunks(host, cam, date) {
  const apiHost = hostForCamera(host, cam);
  try {
    const raw = await fetchJson(`/api/videodb/chunks/${apiHost}/${cam}/${date}?locked_only=0`);
    _vdbChunksByCam[`${host}|${cam}|${date}`] = Array.isArray(raw) ? raw : (raw?.chunks || []);
  } catch (e) {
    _vdbChunksByCam[`${host}|${cam}|${date}`] = [];
  }
  const key = `${host}|${date}`;
  const remaining = (_vdbAllCamsPending.get(key) ?? 1) - 1;
  if (remaining <= 0) {
    _vdbAllCamsPending.delete(key);
    const cams = state.VDB_CAMERAS[host] || [];
    _vdbAllCamsRenderTimeline(host, cams, date);
  } else {
    _vdbAllCamsPending.set(key, remaining);
  }
}

let _vdbResizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(_vdbResizeTimer);
  _vdbResizeTimer = setTimeout(() => {
    if (window.innerWidth < 768 && vdbViewMode === 'all_cams' && _vdbCurrentHost) {
      vdbSwitchView(_vdbCurrentHost, 'single');
    }
    if (_vdbCurrentHost) vdbEnsureViewToggle(_vdbCurrentHost);
  }, 200);
});

function vdbRenderAllCams(host) {
  const cams = state.VDB_CAMERAS[host] || [];
  const container = document.getElementById('vdb-results');
  if (!container) return;
  if (!cams.length) { container.innerHTML = '<div class="no-data">No cameras configured.</div>'; return; }
  const date = vdbGetNight();
  if (!date) { container.innerHTML = '<div class="no-data">No night selected.</div>'; return; }
  _vdbCurrentHost = host;
  container.innerHTML = `<div class="card vdb-allcams-card"><div class="vdb-allcams-scroll"><div class="vdb-allcams-timeline" id="vdb-allcam-global" data-host="${host}" data-date="${date}"><div style="color:var(--muted);font-size:12px;padding:12px">Loading ${cams.length} camera${cams.length !== 1 ? 's' : ''}…</div></div></div></div>`;
  const statusEl = document.getElementById('vdb-status');
  if (statusEl) statusEl.textContent = `${cams.length} camera${cams.length !== 1 ? 's' : ''} · ${fmtDate(date)}`;
  const key = `${host}|${date}`;
  _vdbAllCamsPending.set(key, cams.length);
  for (const cam of cams) vdbFetchAndRenderCamChunks(host, cam, date);
}

/* ─────────────────────────────────────────
   Synced multi-cam modal
───────────────────────────────────────── */

const _VDB_SYNC_WINDOW_S = 300;

function vdbCloseSyncedModal() {
  _getVdbSyncModal().close();
}

async function vdbOpenSyncedModal(host, clickedCam, date, filename, time) {
  const clickedMs = vdbChunkUtcMs(date, time);
  if (clickedMs == null) return;
  const cams = state.VDB_CAMERAS[host] || [];
  if (!cams.length) return;

  // Ensure each cam's chunk list is loaded.
  await Promise.all(cams.map(async cam => {
    const key = `${host}|${cam}|${date}`;
    if (_vdbChunksByCam[key]) return;
    try {
      const apiHost = hostForCamera(host, cam);
      const r = await fetch(`/api/videodb/chunks/${apiHost}/${cam}/${date}?locked_only=0`);
      const raw = await r.json();
      _vdbChunksByCam[key] = Array.isArray(raw) ? raw : (raw?.chunks || []);
    } catch (e) { _vdbChunksByCam[key] = []; }
  }));

  const winMs = _VDB_SYNC_WINDOW_S * 1000;
  const matches = cams.map(cam => {
    const list = _vdbChunksByCam[`${host}|${cam}|${date}`] || [];
    let best = null, bestDelta = Infinity;
    for (const c of list) {
      const ms = vdbChunkUtcMs(date, c.time);
      if (ms == null) continue;
      const delta = Math.abs(ms - clickedMs);
      if (delta <= winMs && delta < bestDelta) { best = c; bestDelta = delta; }
    }
    return { cam, match: best, ms: best ? vdbChunkUtcMs(date, best.time) : null };
  });

  const matched = matches.filter(m => m.ms != null);
  if (!matched.length) return;

  // Reference t=0 = clicked chunk start; offsets = (referenceMs - chunkMs) / 1000.
  // windowSec covers the max spread between matched chunks + one chunk duration.
  const maxSpreadMs = Math.max(...matched.map(m => Math.abs(m.ms - clickedMs)));
  const windowSec   = Math.ceil(maxSpreadMs / 1000) + 22;

  const videos = matches.map(({ cam, match, ms }) => {
    if (!match || ms == null) return null;
    const apiHost = hostForCamera(host, cam);
    const rawSrc = match.source === 'archive'
      ? `/api/archive/file/${cam}/${date}/meteors/${encodeURIComponent(match.filename)}`
      : `/api/cached-video/${apiHost}/${cam}/${date}/${encodeURIComponent(match.filename)}`;
    const url = rawSrc + (rawSrc.includes('?') ? '&' : '?') + 'format=mp4';
    const offset   = (clickedMs - ms) / 1000;
    const stackUrl = match.stack ? `/stack/${apiHost}/${cam}/${date}/${encodeURIComponent(match.stack)}` : null;
    return { cam, station: host, host: apiHost, date, url, offset, label: match.time, filename: match.filename, stackUrl,
             hide: _vdbTileHide(match, cam) };
  });

  _getVdbSyncModal().open({
    title: `All cameras  ·  ${fmtDate(date)}  ·  ${time} UTC`,
    videos: videos.filter(Boolean),
    windowPreSec:  0,
    windowPostSec: windowSec,
    showDetection: false,
    onHiddenChange: _vdbSyncHiddenChange(host, date),
  });
}

/** Open the synced modal from an All Cams timeline card, using only the
 *  cameras that were actually matched and displayed in that card. */
async function vdbOpenTimeSlot(el) {
  const host  = el.dataset.host;
  const date  = el.dataset.date;
  const slots = JSON.parse(el.dataset.slot);  // {cam: {f, t, s}}
  const entries = Object.entries(slots);
  if (!entries.length) return;

  const refMs = vdbChunkUtcMs(date, entries[0][1].t);
  if (refMs == null) return;

  // Ensure chunk cache is populated (needed for the modal's stitch/lock features).
  const cams = state.VDB_CAMERAS[host] || [];
  await Promise.all(cams.map(async cam => {
    const key = `${host}|${cam}|${date}`;
    if (_vdbChunksByCam[key]) return;
    try {
      const apiHost = hostForCamera(host, cam);
      const r   = await fetch(`/api/videodb/chunks/${apiHost}/${cam}/${date}?locked_only=0`);
      const raw = await r.json();
      _vdbChunksByCam[key] = Array.isArray(raw) ? raw : (raw?.chunks || []);
    } catch (e) { _vdbChunksByCam[key] = []; }
  }));

  const maxSpreadMs = Math.max(...entries.map(([, s]) => {
    const ms = vdbChunkUtcMs(date, s.t);
    return ms != null ? Math.abs(ms - refMs) : 0;
  }));
  const windowSec = Math.ceil(maxSpreadMs / 1000) + 22;

  const videos = entries.map(([cam, slot]) => {
    const apiHost = hostForCamera(host, cam);
    const chunkMs = vdbChunkUtcMs(date, slot.t);
    const offset  = chunkMs != null ? (refMs - chunkMs) / 1000 : 0;
    const url     = `/api/cached-video/${apiHost}/${cam}/${date}/${encodeURIComponent(slot.f)}?format=mp4`;
    const stackUrl = slot.s ? `/stack/${apiHost}/${cam}/${date}/${encodeURIComponent(slot.s)}` : null;
    const chunk = (_vdbChunksByCam[`${host}|${cam}|${date}`] || []).find(c => c.filename === slot.f);
    return { cam, station: host, date, url, offset, label: slot.t, filename: slot.f, stackUrl,
             hide: _vdbTileHide(chunk, cam) };
  });

  _getVdbSyncModal().open({
    title:         `All cameras  ·  ${fmtDate(date)}  ·  ${entries[0][1].t} UTC`,
    videos,
    windowPreSec:  0,
    windowPostSec: windowSec,
    showDetection: false,
    onHiddenChange: _vdbSyncHiddenChange(host, date),
  });
}
window.vdbOpenTimeSlot = vdbOpenTimeSlot;

/** Open the All Cams modal for a card and immediately activate dome view. */
async function vdbOpenDome(cardEl) {
  await vdbOpenTimeSlot(cardEl);
  _getVdbSyncModal()._toggleDomeMode();
}
window.vdbOpenDome = vdbOpenDome;


/* ─────────────────────────────────────────
   VDB Select mode
───────────────────────────────────────── */

/* One-way lock (2026-07 hardening): the dashboard can only ADD a manual
   lock — the station API rejects locked:false from the network, so unlock
   is SSH-only. Locked cards render a disabled button and never reach here;
   this always sends locked:true. */
async function vdbToggleLock(station, camera, date, filename, btn) {
  const card    = btn.closest('.vdb-chunk-card');
  btn.disabled  = true;
  try {
    const data = await lockClip(station, camera, date, filename);
    // Update card state in-place: the clip is now locked, so the button
    // becomes the disabled "unlock via SSH" marker.
    card.classList.remove('vdb-chunk-locked-detection', 'vdb-chunk-locked-manual');
    if (data.locked) card.classList.add(`vdb-chunk-locked-${data.lock_type}`);
    btn.classList.remove('vdb-lock-btn--detection', 'vdb-lock-btn--manual', 'vdb-lock-trigger', 'vdb-allcam-lock-trigger');
    if (data.locked) btn.classList.add(`vdb-lock-btn--${data.lock_type}`);
    btn.innerHTML = LOCK_ICON;
    btn.title     = 'Locked — unlock via SSH only';
    btn.setAttribute('aria-label', btn.title);
    // Sync vdbAllChunks so filtering stays consistent
    const chunk = vdbAllChunks.find(c => c.filename === filename);
    if (chunk) { chunk.locked = data.locked; chunk.lock_type = data.lock_type; }
    btn.disabled = true;
  } catch(e) {
    alert('Lock failed: ' + e.message);
    btn.disabled = false;
  }
}

function vdbOpenModal(station, camera, date, filename, time, sizeMb, detOffset = null, idx = -1, videoSrc = '', startInStack = false) {
  _modalOpenedFromRms = false;
  _vdbCurrentIdx = idx;

  const rawSrc = videoSrc || `/api/cached-video/${station}/${camera}/${date}/${encodeURIComponent(filename)}`;
  const src = rawSrc + (rawSrc.includes('?') ? '&' : '?') + 'format=mp4';

  const chunkRec = (window._vdbVisibleChunks || []).find(c => c.filename === filename);
  const rmsDet = window._lookupDetection(filename, chunkRec?.meteor_time);
  // A detection clip can be hidden as a false positive (Hide button, H key).
  const isDetection = !!rmsDet || chunkRec?.lock_type === 'detection';
  _vdbHide = isDetection ? {
    target: hideTarget(rmsDet, camera) || hideTarget(chunkRec, camera),
    hidden: !!(chunkRec?.hidden || rmsDet?.hidden),
    page: _modalOpenedFromRms || window._modalOpenedFromRms ? 'detections' : 'video',
    onChange: (resp) => {
      _vdbHide.hidden = !!resp?.hidden;
      window.rmsOnHiddenChange?.(date, filename, resp);
      if (state.activeTab === 'videodb') vdbOnHiddenChange(filename, resp);
    },
  } : null;

  // The shared meteor window (CLIP_PRE_S / CLIP_POST_S); the modal clamps the
  // end to the clip's real length (a stitched clip runs up to 40 s).
  const trimStart = detOffset != null ? Math.max(0, detOffset - CLIP_PRE_S) : 0;
  const trimEnd   = detOffset != null ? detOffset + CLIP_POST_S : undefined;

  const n = (window._vdbVisibleChunks || []).length;
  const arrayDelta_prev = window.vdbSortNewest ? 1 : -1;
  const arrayDelta_next = window.vdbSortNewest ? -1 : 1;
  const stitchEarlierIdx = window.vdbSortNewest ? idx + 1 : idx - 1;
  const stitchLaterIdx   = window.vdbSortNewest ? idx - 1 : idx + 1;
  const navPrevIdx = idx + arrayDelta_prev;
  const navNextIdx = idx + arrayDelta_next;

  const isArchive = Boolean(videoSrc && videoSrc.startsWith('/api/archive/'));
  let stack = null;
  if (chunkRec?.stack && !isArchive) {
    stack = { url: `/fullstack/${station}/${camera}/${date}/${encodeURIComponent(chunkRec.stack)}` };
  }

  _getVdbModal().open({
    src,
    title: `${camera}  ·  ${fmtDate(date)}  ·  ${time} UTC`,
    station, camera, date, filename,
    locked:   chunkRec?.locked,
    lockType: chunkRec?.lock_type,
    onLocked: (data) => {
      // Reflect the new lock in the grid data so closing the modal shows the
      // clip already locked (mirrors vdbToggleLock's in-place card update).
      if (chunkRec) { chunkRec.locked = data.locked; chunkRec.lock_type = data.lock_type; }
      const inAll = vdbAllChunks.find(c => c.filename === filename);
      if (inAll) { inAll.locked = data.locked; inAll.lock_type = data.lock_type; }
      if (!window._modalOpenedFromRms) vdbRenderResults();
    },
    loopDlPath: isArchive
      ? `/loop_clip_archive/${camera}/${date}/${encodeURIComponent(filename)}`
      : `/loop_clip/${station}/${camera}/${date}/${encodeURIComponent(filename)}`,
    detOffset,
    trimStart,
    trimEnd,
    detection: rmsDet || null,
    hide: _vdbHide,
    stack,
    startInStack,
    // "Download clip" = the original chunk (archive copy first, else the
    // station's); the trim / stitch cut is "Download trimmed".
    download: {
      originalUrl: /\.mkv$/.test(filename || '')
        ? `/download/${station}/${camera}/${date}/${encodeURIComponent(filename)}`
        : null,
      onClick: _vdbDownload,
    },
    stitch: {
      hasPrev:    idx >= 0 && stitchEarlierIdx >= 0 && stitchEarlierIdx < n,
      hasNext:    idx >= 0 && stitchLaterIdx   >= 0 && stitchLaterIdx   < n,
      onPrev:     () => vdbStitchAdj(-1),
      onNext:     () => vdbStitchAdj(1),
      onUnstitch: () => vdbUnstitch(),
    },
    nav: {
      hasPrev: idx >= 0 && navPrevIdx >= 0 && navPrevIdx < n,
      hasNext: idx >= 0 && navNextIdx >= 0 && navNextIdx < n,
      onPrev:  () => vdbNavModal(-1),
      onNext:  () => vdbNavModal(1),
    },
  });

  const video = _vdbModal.getVideoEl();
  if (video) {
    video.addEventListener('canplay', () => {
      if (!vdbCachedFiles.has(filename)) {
        vdbCachedFiles.add(filename);
        vdbRenderResults();
      }
    }, { once: true });
  }
}

async function _vdbDownload(btn) {
  const modal = _getVdbModal();
  const ts = modal.getTrimState();
  if (!ts) return;
  const dlBtn = btn || modal._el?.querySelector('.vm-download-btn');
  const label = dlBtn ? dlBtn.innerHTML : null;
  if (dlBtn) { dlBtn.disabled = true; dlBtn.textContent = 'Preparing…'; }
  try {
    const { station, camera, date, filename, filename2, stitched, stitchAdjIsLater, start, end } = ts;
    let url;
    if (stitched && filename2) {
      // Stitch route already carries f1/f2 query params, so append ss/t
      // directly rather than through buildTrimmedShortclipUrl (which owns the
      // leading '?').
      const [f1, f2] = stitchAdjIsLater ? [filename, filename2] : [filename2, filename];
      url = `/shortclip_stitch/${station}/${camera}/${date}?f1=${encodeURIComponent(f1)}&f2=${encodeURIComponent(f2)}&ss=${start.toFixed(2)}&t=${(end - start).toFixed(2)}`;
    } else {
      const base = `/shortclip/${station}/${camera}/${date}/${encodeURIComponent(filename)}`;
      url = buildTrimmedShortclipUrl(base, ts).url;
    }
    const resp = await fetch(url);
    if (!resp.ok) {
      _toast(resp.status === 404 ? 'Clip no longer available on station' : `Download failed (${resp.status})`, 'error');
      return;
    }
    noteUntrimmed(resp);
    const cd = resp.headers.get('content-disposition');
    let dlFilename = 'clip.mp4';
    if (cd) { const m = cd.match(/filename="?([^";\n]+)"?/); if (m) dlFilename = m[1]; }
    saveBlob(await resp.blob(), dlFilename);
  } catch (e) {
    _toast('Download failed: network error', 'error');
  } finally {
    if (dlBtn) { dlBtn.disabled = false; dlBtn.innerHTML = label; }
  }
}

function vdbCloseModal(e) {
  _getVdbModal().close();
}

// The open clip's hide state (vdbOpenModal), for the H key.
let _vdbHide = null;

/** A hide or unhide of the Video DB clip ``filename`` (card or player): mark
 *  the chunk and re-render; the card stays, dimmed or not (footage list). */
function vdbOnHiddenChange(filename, resp) {
  const chunk = vdbAllChunks.find(c => c.filename === filename);
  if (!chunk) return;
  applyHidden(chunk, resp);
  vdbRenderResults();
}

/** The Hide of a synced (all cameras) tile: only a detection clip has one. */
function _vdbTileHide(chunk, cam) {
  if (!chunk || chunk.lock_type !== 'detection' || !chunk.meteor_time) return undefined;
  return { target: hideTarget(chunk, cam), hidden: !!chunk.hidden };
}

/** A hide in the synced modal marks the tile's chunk in the per-camera cache
 *  (so a reopened modal shows it) and, for the camera the grid shows, the
 *  card too. */
function _vdbSyncHiddenChange(host, date) {
  return (tile, resp) => {
    const chunk = (_vdbChunksByCam[`${host}|${tile.cam}|${date}`] || []).find(c => c.filename === tile.filename);
    if (chunk) applyHidden(chunk, resp);
    if (tile.cam === vdbGetSelectedCamera()) vdbOnHiddenChange(tile.filename, resp);
  };
}

/** H in the clip player: hide the open detection and go to the next clip,
 *  for going through a magnitude-sorted list quickly. The modal's own Hide
 *  button stays the way to unhide. */
function vdbHideAndNext() {
  const h = _vdbHide;
  if (!h?.target || h.hidden || !window.vdbModalIsOpen?.()) return false;
  const btn = _vdbModal?._el?.querySelector('.vm-hide-btn');
  if (!btn || btn.style.display === 'none') return false;
  toggleHide(h.target, true, h.onChange, { page: h.page });
  vdbNavModal(1);
  return true;
}

function vdbNavModal(delta) {
  if (!window._vdbVisibleChunks.length || _vdbCurrentIdx < 0) return;
  const arrayDelta = _modalOpenedFromRms ? delta : (window.vdbSortNewest ? -delta : delta);
  const newIdx = Math.max(0, Math.min(window._vdbVisibleChunks.length - 1, _vdbCurrentIdx + arrayDelta));
  if (newIdx === _vdbCurrentIdx) return;
  const c = window._vdbVisibleChunks[newIdx];
  const ts = _vdbModal?.getTrimState();
  const station = ts?.station || '';
  const camera  = ts?.camera  || '';
  const date = c._date || ts?.date || '';
  const detOff = c.lock_type === 'detection' ? (c.detection_offset_s ?? null) : null;
  vdbOpenModal(station, camera, date, c.filename, c.time, c.size_mb, detOff, newIdx);
}

async function vdbProcessChunk(host, cam, date, filename, btn) {
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = '\u23f3';
  try {
    const r = await fetch(`/api/encode_chunk/${host}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({camera: cam, date, filename}),
    });
    const data = await r.json();
    if (!r.ok || !data.ok) throw new Error(data.error || 'encode returned ok:false');
    const chunk = vdbAllChunks.find(c => c.filename === filename);
    if (chunk) chunk.reencoded = true;
    btn.textContent = '\u2713 Processed';
    btn.className = 'vdb-process-btn vdb-process-btn--done';
    btn.disabled = true;
    btn.onclick = null;
    setTimeout(() => vdbRenderResults(), 800);
  } catch (err) {
    btn.textContent = '\u2717';
    btn.title = err.message || 'Failed';
    setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 3000);
  }
}

async function vdbStitchAdj(chronoDelta) {
  const arrayDelta = window.vdbSortNewest ? -chronoDelta : chronoDelta;
  const adjIdx = _vdbCurrentIdx + arrayDelta;
  if (adjIdx < 0 || adjIdx >= window._vdbVisibleChunks.length) return;
  const adj = window._vdbVisibleChunks[adjIdx];
  const modal = _getVdbModal();
  const ts = modal.getTrimState();
  if (!ts) return;
  const { station, camera, date, filename } = ts;
  const [f1, f2] = chronoDelta > 0 ? [filename, adj.filename] : [adj.filename, filename];

  modal.updateStitch({ isStitched: true, label: 'Stitching…' });

  const video = modal.getVideoEl();
  const src = `/stitch_video/${station}/${camera}/${date}?f1=${encodeURIComponent(f1)}&f2=${encodeURIComponent(f2)}&format=mp4`;
  video.src = src;
  video.load();
  video.play().catch(() => {});

  const stitchAt = chronoDelta > 0 ? (ts.duration || 20) : 0;
  modal.updateTrim({
    stitched: true,
    filename2: adj.filename,
    stitchAdjIsLater: chronoDelta > 0,
    stitchAt,
    start: Math.max(0, stitchAt - 5),
    end:   stitchAt + 10,
  });

  video.addEventListener('loadedmetadata', () => {
    const dur = isFinite(video.duration) && video.duration > 0 ? video.duration : 40;
    modal.updateTrim({ duration: dur, stitchAt: dur / 2, end: Math.min(dur, stitchAt + 10) });
  }, { once: true });

  modal.updateStitch({ isStitched: true, label: `Stitched with ${chronoDelta > 0 ? 'next' : 'prev'}` });
}

function vdbUnstitch() {
  const modal = _getVdbModal();
  const ts = modal.getTrimState();
  if (!ts) return;
  const { station, camera, date, filename, detOffset, videoSrc } = ts;

  modal.updateTrim({ stitched: false, filename2: null, stitchAt: null });

  const video = modal.getVideoEl();
  const rawSrc = videoSrc || `/api/cached-video/${station}/${camera}/${date}/${encodeURIComponent(filename)}`;
  video.src = rawSrc + (rawSrc.includes('?') ? '&' : '?') + 'format=mp4';
  video.load();
  video.play().catch(() => {});
  video.addEventListener('loadedmetadata', () => {
    const dur = isFinite(video.duration) && video.duration > 0 ? video.duration : 20;
    modal.updateTrim({
      duration: dur,
      start: detOffset != null ? Math.max(0, detOffset - CLIP_PRE_S) : 0,
      end:   detOffset != null ? Math.min(dur, detOffset + CLIP_POST_S) : dur,
    });
  }, { once: true });

  const n = (window._vdbVisibleChunks || []).length;
  const stitchEarlierIdx = window.vdbSortNewest ? _vdbCurrentIdx + 1 : _vdbCurrentIdx - 1;
  const stitchLaterIdx   = window.vdbSortNewest ? _vdbCurrentIdx - 1 : _vdbCurrentIdx + 1;
  modal.updateStitch({
    hasPrev: stitchEarlierIdx >= 0 && stitchEarlierIdx < n,
    hasNext: stitchLaterIdx   >= 0 && stitchLaterIdx   < n,
    isStitched: false,
  });
}

// Expose to global scope for inline onclick handlers
window.vdbCloseModal = vdbCloseModal;
window.vdbNavModal = vdbNavModal;
window.vdbHideAndNext = vdbHideAndNext;
window.vdbOnHiddenChange = vdbOnHiddenChange;
window.vdbStitchAdj = vdbStitchAdj;
window.vdbToggleSort = vdbToggleSort;
window.vdbUnstitch = vdbUnstitch;
window.vdbOpenSyncedModal = vdbOpenSyncedModal;
window.vdbSetAllCamsSort = vdbSetAllCamsSort;
window.vdbSwitchView = vdbSwitchView;
window.vdbStopPoll = vdbStopPoll;
window.vdbInit = vdbInit;
window.vdbLoadAzimuths = vdbLoadAzimuths;
window.hostForCamera = hostForCamera;
window.vdbCloseSyncedModal = vdbCloseSyncedModal;
window.vdbModalIsOpen = () => !!_vdbModal?._el?.classList.contains('open');
window.vdbPoll = vdbPoll;
window.vdbOpenModal = vdbOpenModal;
window.vdbProcessChunk = vdbProcessChunk;
window.vdbToggleLock = vdbToggleLock;
window.vdbSliderInput = vdbSliderInput;
window.vdbTimeInputChange = vdbTimeInputChange;
window.vdbUpdateZoom = vdbUpdateZoom;
window.vdbOnNightChange = vdbOnNightChange;
window.vdbPickNight = vdbPickNight;
window.vdbMatchesView = vdbMatchesView;
window.vdbResetTimeRange = vdbResetTimeRange;
window.vdbOnLockedOnlyChange = vdbOnLockedOnlyChange;
window._vdbAzCompass = _vdbAzCompass;
