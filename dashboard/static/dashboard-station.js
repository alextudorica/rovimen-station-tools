import { computeCaptureWindow, fmtCaptureUTC, clampDateInputsToToday, fmtBytes, fmtDate, barColor, fetchJson, appPanelClose, _modalOpen, _modalClose, _closeAllOpenModals, escHtml, serviceIsUp, serviceStateClass, state, segmentHealthBadge, parseViewParams, viewQuery, VIEW_KEYS, createUrlSync, fetchAuthStatus, nightTitle } from './dashboard-common.js';
/* global renderStationDropdown, updateStationDots, rmsPlotExpand,
          vdbCloseModal, arcModalClose, vdbNavModal */
/*
 * dashboard-station.js
 * ────────────────────
 * Per-station detail page (dashboard.html): tabs, RMS detection grid,
 * Video Database, Archive, services + storage cards, live feed modals.
 * The System Admin tab is dashboard-sysadmin.js: this module fetches its
 * data and calls window.renderSysAdmin, and puts the helpers the tab needs
 * on window (end of file).
 *
 * Depends on dashboard-common.js (formatters, app-panel, state.STATIONS_META,
 * globals on `window`).
 */

/* ─────────────────────────────────────────
   askConfirm(message, onConfirm, opts) — in-DOM replacement for the
   blocking window.confirm() used by the remaining confirm-gated action
   (one-way clip lock; station mutation controls were removed with the
   read-only station API). The native confirm() halts timers, SSE
   streams and live updates while the user decides, and on iOS it cannot
   be styled. This helper renders a card
   styled like .cam-modal/.vdb-modal, keeps the event loop running, and
   exposes Cancel + Confirm buttons that meet the 44 px tap target.

   Signature:
     askConfirm(message, onConfirm, {
       confirmLabel?: string = 'Confirm',
       cancelLabel?:  string = 'Cancel',
       destructive?: boolean = true,   // red Confirm vs neutral blue
       title?:        string,           // optional bold heading line
       onCancel?:     () => void,
     })

   Only one askConfirm card is mounted at a time; opening a second
   replaces the first. Escape / clicking the backdrop cancels.
───────────────────────────────────────── */
function askConfirm(message, onConfirm, opts) {
  opts = opts || {};
  const confirmLabel = opts.confirmLabel || 'Confirm';
  const cancelLabel  = opts.cancelLabel  || 'Cancel';
  const destructive  = opts.destructive !== false;
  const title        = opts.title || '';

  // Remove any prior card so we don't stack overlays.
  document.querySelectorAll('.ask-confirm').forEach(n => n.remove());

  const card = document.createElement('div');
  card.className = 'ask-confirm open';
  card.setAttribute('role', 'dialog');
  card.setAttribute('aria-modal', 'true');

  const titleHtml = title
    ? `<div style="font-weight:600;margin-bottom:6px">${escHtml(title)}</div>`
    : '';
  card.innerHTML = `
    <div class="ask-confirm-card">
      <div class="ask-confirm-body">
        ${titleHtml}
        <div>${escHtml(message)}</div>
      </div>
      <div class="ask-confirm-actions">
        <button type="button" class="ask-confirm-btn ask-confirm-cancel">${escHtml(cancelLabel)}</button>
        <button type="button" class="ask-confirm-btn ask-confirm-ok${destructive ? '' : ' neutral'}">${escHtml(confirmLabel)}</button>
      </div>
    </div>`;
  document.body.appendChild(card);

  const okBtn     = card.querySelector('.ask-confirm-ok');
  const cancelBtn = card.querySelector('.ask-confirm-cancel');

  function cleanup() {
    document.removeEventListener('keydown', onKey, true);
    card.remove();
  }
  function fireCancel() {
    cleanup();
    try { opts.onCancel && opts.onCancel(); } catch(e) { /* swallow */ }
  }
  function fireOk() {
    cleanup();
    try { onConfirm(); } catch(e) { /* caller logs */ }
  }
  function onKey(e) {
    if (e.key === 'Escape') { e.stopPropagation(); fireCancel(); }
    else if (e.key === 'Enter') { e.stopPropagation(); fireOk(); }
  }

  okBtn.addEventListener('click', fireOk);
  cancelBtn.addEventListener('click', fireCancel);
  card.addEventListener('click', (e) => { if (e.target === card) fireCancel(); });
  document.addEventListener('keydown', onKey, true);

  // Focus Confirm so keyboard users can hit Enter immediately; cancel
  // gets the visual default outline.
  setTimeout(() => { try { okBtn.focus(); } catch(e) {} }, 0);
}

/* ─────────────────────────────────────────
   Station-only state. Cross-bundle state (state.STATIONS_META, state.statusData,
   state.vitalsData, state.activeStation, state.activeTab, etc.) lives in dashboard-common.js
   and is read here via the `var` aliases that common.js exposes.
───────────────────────────────────────── */
window.settingsData  = {};   // host -> config object (window-scoped for cross-module access)
window.encStatsData  = {};   // host -> encoding stats from station API
window.hwData        = {};   // host -> hardware profile
let liveActive    = {};   // `${host}_${camCode}` -> bool
let liveAutoStop  = {};   // `${host}_${camCode}` -> { deadline, timeoutId, intervalId }
let _liveRetries  = {};   // `${host}_${camCode}` -> int (error count)

// Live view eats encoder cycles + ifb bandwidth on the camera. While RMS is
// actively capturing meteors (rms-cam* services running), keeping a stream
// open causes frame drops. Auto-stop the feed after 60 s with a visible
// countdown so an opened stream can never be left running overnight.
const LIVE_VIEW_MAX_S = 60;
window._trimState    = {};   // current modal detection trim state
window._trimRaf      = null; // requestAnimationFrame id for playhead

let sysadminVitalsTimer  = null; // 10s fetch-then-reschedule while System Admin tab is active
let sysadminStorageTimer = null; // 30s storage refresh while System Admin tab is active
let nightTimer           = null;
let vdbSkyDomeTimer      = null; // 60s BW sky-dome cache-bust while Video DB tab is active
window.vdbAllCamsSortDesc   = true; // chunk order per row in All cams view; true = newest first.
                                 // P1-34: single-cam window.vdbSortNewest mirrors this value at
                                 // station-switch + toggle time so the two views can't disagree.

// Sky-dome freshness indicator state — captureMs is the newest FF capture
// time across cameras the last server-side render used. Refreshed via
// /api/sky_dome_meta on the same 60 s cadence as the dome image itself.
const _domeFreshness = {};       // host -> {captureMs: number|null}
let _domeFreshTicker = null;     // 1 s "Now" clock ticker

// System Admin tab data, filled here and drawn by dashboard-sysadmin.js
window.vitalsHistory     = {};  // host -> ring buffer of vitals snapshots
window.coreData          = {};  // host -> [{load_pct, freq_ghz}]
window.cronsData         = {};  // host -> [{schedule, command, source}]
window.storagewatchData  = {};  // host -> {last_run, deleted_gb, filesystems}

/* ─────────────────────────────────────────
   Inflight fetch dedupe / abort
   ─────────────────────────────────────────
   Rapid tab-switches used to fire identical fetches in parallel, where the
   older response could clobber the newer one. fetchOnce(key, ...) aborts any
   prior request for `key` before issuing the next. abortAllInflight('tab:')
   is called on tab change so background tab fetches stop holding sockets
   open. Callers must swallow AbortError on the awaited fetch.
───────────────────────────────────────── */
const _inflight = new Map();
function fetchOnce(key, url, options = {}) {
  const prev = _inflight.get(key);
  if (prev) prev.controller.abort();
  const controller = new AbortController();
  const p = fetch(url, { ...options, signal: controller.signal })
    .finally(() => {
      if (_inflight.get(key)?.controller === controller) _inflight.delete(key);
    });
  _inflight.set(key, { controller, promise: p });
  return p;
}
function abortAllInflight(prefix) {
  for (const [k, v] of _inflight) {
    if (!prefix || k.startsWith(prefix)) v.controller.abort();
  }
}
export function _isAbort(e) { return e && e.name === 'AbortError'; }

/* ─────────────────────────────────────────
   Station / Tab switching
   state.view (camera, night, time window) is mirrored into the query of
   /station/<host>/<tab>. Tab and station switches pushState; view changes
   inside a tab replaceState through stationSyncUrl, so Back steps through
   tabs and stations and each entry reopens the view it was left on.
───────────────────────────────────────── */
const stationSyncUrl = createUrlSync(() => viewQuery(state.view));
window.stationSyncUrl = stationSyncUrl;

function _stationUrl(host, tab) {
  return `/station/${host}/${tab}${viewQuery(state.view)}`;
}

/** state.view from the current URL, keeping the camera only when `host`
 *  has it. */
function _viewFromUrl(host) {
  const v = parseViewParams(window.location.search);
  if (v.cam && !(state.VDB_CAMERAS[host] || []).includes(v.cam)) v.cam = null;
  return v;
}

/** True when the URL's cam/date/from/to is the view on screen. */
function _urlShowsView(host) {
  const v = _viewFromUrl(host);
  return VIEW_KEYS.every(k => (v[k] ?? null) === (state.view[k] ?? null));
}

function selectStation(host) {
  if (host === state.activeStation) return;
  // Any modal left open from the previous station (live cam, RMS plot,
  // VDB clip, app panel) must close before we swap content — otherwise
  // a player or plot lingers on top of the new station's pane.
  _closeAllOpenModals();
  // Keep the night and time window; only the camera belongs to the old
  // station. The RMS tab re-scrolls to the same night on the new station.
  if (state.activeTab === 'rms') window.rmsCaptureView?.();
  state.view.cam = null;
  if (state.activeTab === 'rms') window._rmsPendingScrollDate = state.view.date;
  state.activeStation = host;
  if (window.renderStationDropdown) renderStationDropdown();
  const _m = state.STATIONS_META[host];
  if (_m) updateNextCapture(_m.lat, _m.lon);
  paintLastUpdated();
  updateTabVisibility(host);
  window._vdbAzimuths = {};
  window.vdbLoadAzimuths?.(host);

  // Clear per-tab timers so switchTab restarts them fresh for the new station
  if (nightTimer)          { clearInterval(nightTimer);          nightTimer          = null; }
  if (sysadminVitalsTimer) { clearTimeout(sysadminVitalsTimer);  sysadminVitalsTimer = null; }
  if (sysadminStorageTimer){ clearInterval(sysadminStorageTimer);sysadminStorageTimer= null; }
  if (vdbSkyDomeTimer)     { clearInterval(vdbSkyDomeTimer);     vdbSkyDomeTimer     = null; }
  if (_domeFreshTicker)    { clearInterval(_domeFreshTicker);    _domeFreshTicker    = null; }

  // Full tab re-init for the new station (also updates the URL)
  const tabBtn = document.getElementById('tab-btn-' + state.activeTab);
  if (tabBtn) switchTab(state.activeTab, tabBtn);

  // Fetch fresh data for this station if we have none
  if (!state.statusData[host]) fetchStatus(host);
  if (!state.vitalsData[host]) fetchVitals(host);
}

// A tab clicked before init has resolved the station (switchTab is on
// window from module load, init awaits /api/stations). Init opens it.
let _tabBeforeInit = null;

function switchTab(tab, btn, { push = true } = {}) {
  // Without a station there is nothing to switch to yet: pushing here wrote
  // /station/null/<tab>, which 404s on reload (seen 2026-09-04 and -14 from
  // the map on a slow load).
  if (!state.activeStation) {
    _tabBeforeInit = tab;
    return;
  }
  // Any modal left open from the previous tab (RMS plot, VDB clip,
  // archive playback, app panel) must close before we swap panes —
  // otherwise it lingers on top of the new tab's content.
  _closeAllOpenModals();
  if (tab !== 'videodb' && window.vdbStopPoll) window.vdbStopPoll();
  if (tab !== 'settings' && window.logsState?.live) { clearInterval(window.logsState.timer); window.logsState.timer = null; window.logsState.live = false; }
  if (tab !== 'settings' && nightTimer)          { clearInterval(nightTimer);          nightTimer          = null; }
  if (tab !== 'settings' && sysadminVitalsTimer) { clearTimeout(sysadminVitalsTimer);  sysadminVitalsTimer = null; }
  if (tab !== 'settings' && sysadminStorageTimer){ clearInterval(sysadminStorageTimer);sysadminStorageTimer= null; }
  if (tab !== 'videodb' && vdbSkyDomeTimer)      { clearInterval(vdbSkyDomeTimer);     vdbSkyDomeTimer     = null; }
  if (tab !== 'videodb' && _domeFreshTicker)     { clearInterval(_domeFreshTicker);    _domeFreshTicker    = null; }
  if (tab !== state.activeTab) abortAllInflight('tab:');
  // Leaving the RMS tab: pin the night in view so the next tab opens it.
  // Entering it: scroll to the pinned night.
  if (state.activeTab === 'rms' && tab !== 'rms') window.rmsCaptureView?.();
  if (tab === 'rms' && state.activeTab !== 'rms' && state.view.date) {
    window._rmsPendingScrollDate = state.view.date;
  }
  state.activeTab = tab;
  document.querySelectorAll('.sub-tab').forEach(b => {
    b.classList.remove('active');
    b.setAttribute('aria-selected', 'false');
    b.tabIndex = -1;
  });
  btn.classList.add('active');
  btn.setAttribute('aria-selected', 'true');
  btn.tabIndex = 0;
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.getElementById('pane-' + tab).classList.add('active');
  if (push) {
    stationSyncUrl.flush();
    history.pushState({ station: state.activeStation, tab }, '', _stationUrl(state.activeStation, tab));
  }
  // Renders the RMS, settings, FDP and spectro panes; the lazy loads below
  // cover the rest.
  renderActiveTab();

  // Lazy loads on tab open
  if (tab === 'settings') {
    if (!window.settingsData[state.activeStation]) fetchSettings(state.activeStation);
    else window.renderSysAdmin?.(state.activeStation);
    // The night is read from the archive (VPS-local), so a minute is cheap;
    // a station pushes its state.json about every 20 minutes.
    if (!nightTimer) {
      nightTimer = setInterval(() => {
        if (state.activeTab === 'settings') window.refreshNightIfShown?.(state.activeStation);
      }, 60000);
    }
    if (!sysadminVitalsTimer) {
      (function scheduleVitalsPoll() {
        sysadminVitalsTimer = setTimeout(async () => {
          if (state.activeTab === 'settings') {
            await fetchVitalsFast(state.activeStation);
          }
          if (state.activeTab === 'settings') scheduleVitalsPoll();
          else sysadminVitalsTimer = null;
        }, 10000);
      })();
    }
    if (!sysadminStorageTimer) {
      sysadminStorageTimer = setInterval(() => {
        if (state.activeTab === 'settings') fetchStorageForSysAdmin(state.activeStation);
      }, 30000);
    }
  }
  if (tab === 'videodb') {
    // Reload when the station changed or another tab moved the camera,
    // night or time window since the Video DB was last shown.
    if (window.vdbInit && (window.vdbLoadedStation !== state.activeStation || !window.vdbMatchesView?.())) {
      window.vdbInit();
    }
    // Idempotent mount — vdbMountSkyDome() bails out if the card is already
    // there for the active station, so re-entering the tab doesn't
    // re-create the DOM node (and date-picker changes don't either, since
    // they only repaint #vdb-results, not the pane root).
    vdbMountSkyDome(state.activeStation);
    domeFetchFreshness(state.activeStation);
    domeEnsureFreshTicker();
    if (!vdbSkyDomeTimer) {
      // Live BW dome — 60 s cache-bust so the newest FF maxpixel rolls in
      // while the user is browsing the Video DB tab. Always shows
      // tonight's sky regardless of which date the user picked below.
      vdbSkyDomeTimer = setInterval(() => {
        if (state.activeTab === 'videodb') vdbRefreshSkyDome(state.activeStation);
      }, 60000);
    }
  }
  if (tab === 'fdp') {
    renderFinalDataProducts(state.activeStation);
    if (!state.tlData[state.activeStation]) fetchTimelapses(state.activeStation);
  }
  if (tab === 'archive') {
    window.archiveInit?.();
  }
  if (tab === 'spectro') {
    window.renderSpectro?.(state.activeStation);
  }
}

function renderActiveTab() {
  const host = state.activeStation;
  if (state.activeTab === 'rms')       window.renderRMS?.(host);
  if (state.activeTab === 'settings')  window.renderSysAdmin?.(host);
  if (state.activeTab === 'fdp')       renderFinalDataProducts(host);
  if (state.activeTab === 'spectro')   window.renderSpectro?.(host);
}

/* ─────────────────────────────────────────
   Services card
───────────────────────────────────────── */
function mkServices(services) {
  const names = {
    'rovimen-nightwatcher': 'Nightwatcher',
    'rovimen-coppermind':   'Coppermind',
    'color-capture':        'Colour capture',
    'rovimen-tineye':       'Tineye',
    'rovimen-station-api':  'Station API',
  };
  // rms-cam* -> camera code, from the station's camera list
  const camLookup = {};
  const meta = state.STATIONS_META[state.activeStation];
  if (meta) {
    meta.cameras.forEach((c, i) => { camLookup[`rms-cam${i + 1}`] = c.code; });
    // rms-capture (single-process RMS) covers every camera
    if (!camLookup['rms-cam1']) camLookup['rms-capture'] = meta.cameras.map(c => c.code).join(', ');
  }
  // The words carry the state; the dot (shape and colour) repeats it.
  const stateText = {
    active: 'Running', failed: 'Failed', inactive: 'Stopped',
    unmanaged: 'Running outside systemd', unknown: 'Unknown',
  };
  const rows = Object.entries(services).map(([svc, st]) => {
    const cls = serviceStateClass(st);
    const code = camLookup[svc];
    const name = code ? `RMS ${code}` : (names[svc] || svc);
    return { rms: !!code || svc.startsWith('rms-'), html: `<li class="sa-svc s-${cls}">
      <span class="dot dot-${cls}" aria-hidden="true"></span><span class="sa-mono">${escHtml(name)}</span><span>${stateText[cls]}</span></li>` };
  });
  // RMS first: it is what must never stop.
  rows.sort((x, y) => y.rms - x.rms);
  return rows.length ? `<ul class="sa-list">${rows.map(r => r.html).join('')}</ul>` : '<span class="sa-note">No services reported</span>';
}

/* ─────────────────────────────────────────
   Storage card
───────────────────────────────────────── */
// The janitor's "escalation could not relieve the disk" episode
// (status.disk_pressure, from janitor_state.json). On gmnro14 (Chiribiș) it
// was logged ~120 times a day for five days before the disk hit 100% and hung
// RMS on 2026-10-16; nothing showed it. Absent on older bundles → no banner.
export function mkDiskPressure(pressure) {
  if (!pressure || !pressure.since) return '';
  const since = new Date(pressure.since);
  const hours = Math.max(0, (Date.now() - since) / 3600000);
  const dur = isNaN(hours) ? '' : hours < 1 ? `${Math.round(hours * 60)} min` : `${hours.toFixed(1)} h`;
  const pct = pressure.pct != null ? `${pressure.pct}%` : '?';
  const free = pressure.free_gb != null ? `${pressure.free_gb} GB free` : '';
  return `<div class="disk-pressure sa-alert sa-alert-bad">
    <strong>Disk full: the janitor cannot free enough space</strong>
    <div>${escHtml(pressure.filesystem || '')} at ${pct}${free ? ' · ' + free : ''}${dur ? ' · for ' + dur : ''}.
      RMS stops recording and hangs when the disk fills.</div>
  </div>`;
}

// Colour capture diverted to the fallback root on the station's own disk
// (status.capture_fallback): the colour disk is missing or unwritable, so
// colour runs on short retention until it is back. Absent on older bundles.
export function mkCaptureFallback(fb) {
  if (!fb || !fb.active) return '';
  return `<div class="capture-fallback sa-alert sa-alert-warn">
    <strong>Colour disk missing: capturing to the internal disk</strong>
    <div>${escHtml(fb.configured || '')} is not writable; colour goes to ${escHtml(fb.active)} with short retention until the disk is back.</div>
  </div>`;
}

function mkStorage(storage, disk, extraDisks, pressure, fallback, thresholds = {}) {
  // If the station has no usable storage telemetry at all (psutil missing on
  // the station-side probe), an empty grid of "—" reads as "no storage" not
  // "metrics unavailable". Surface that explicitly.
  const haveStorage = storage && Object.values(storage).some(v => v && v.bytes != null);
  const haveDisk    = disk && (disk.used_mb != null || disk.total_mb != null);
  const haveExtras  = Array.isArray(extraDisks) && extraDisks.some(ed => ed && (ed.used_mb != null || ed.total_mb != null));
  if (!haveStorage && !haveDisk && !haveExtras) {
    return `<div class="sa-alert sa-alert-warn"><strong>Disk metrics unavailable on this station.</strong>
      <div>The station-side probe couldn't read filesystem usage (psutil missing or storage probe failing).
        Other telemetry is still being collected.</div></div>`;
  }

  // Ticks at the janitor's cleanup thresholds, so the bar shows how close a
  // disk is to the next cleanup step.
  const ticks = [['warn_pct', 85, 'Warn'], ['nuclear_pct', 90, 'Aggressive cleanup'], ['extreme_pct', 95, 'Emergency cleanup']]
    .map(([k, dflt, label], i) => {
      const v = thresholds?.[k] ?? dflt;
      return `<i class="sa-tick sa-tick-${i}" style="left:${v}%" title="${label} at ${v}%"></i>`;
    }).join('');
  // Guard every numeric field: a station can return `disk` with only
  // `device` and null usage (offline-but-cached shells, a probe that crashed
  // mid-collect).
  const diskRow = (d, label) => {
    if (!d) return '';
    const gb = mb => mb != null ? (mb / 1024).toFixed(1) + ' GB' : '—';
    const pct = d.pct ?? 0;
    return `<div class="sa-disk">
      <div class="sa-disk-meta"><span class="sa-mono sa-strong">${escHtml(label)}</span>
        <span class="sa-mono">${gb(d.used_mb)} <span class="sa-unit">of ${gb(d.total_mb)}${d.pct != null ? `, ${d.pct}%` : ''}</span></span></div>
      <div class="sa-disk-track"><div class="sa-disk-fill" style="width:${pct}%;background:${barColor(d.pct)}"></div>${ticks}</div>
    </div>`;
  };
  let disks = disk ? diskRow(disk, disk.device || 'System disk') : '';
  for (const ed of (extraDisks || [])) disks += diskRow(ed, `${ed.source} → ${ed.mount}`);

  const folders = [['color_capture', 'Colour video'], ['color_timelapse', 'Timelapses'], ['rms', 'RMS captures and archive']]
    .map(([k, label]) => {
      const d = storage?.[k];
      return `<tr><th scope="row">${label}</th>
        <td class="sa-mono">${d ? fmtBytes(d.bytes) : '—'}</td>
        <td class="sa-mono">${d?.days ?? '—'}</td>
        <td class="sa-mono">${d ? fmtDate(d.oldest) : '—'}</td></tr>`;
    }).join('');
  const other = disk?.other_mb != null
    ? `<tr><th scope="row">Other (OS and the rest)</th><td class="sa-mono">${fmtBytes(disk.other_mb * 1024 * 1024)}</td><td></td><td></td></tr>` : '';
  return `${mkCaptureFallback(fallback)}${mkDiskPressure(pressure)}
    <div class="sa-stack sa-disks">${disks}</div>
    <p class="sa-note">The ticks are the cleanup thresholds: warn, aggressive, emergency.</p>
    <table class="sa-table"><thead><tr><th scope="col">Folder</th><th scope="col">Size</th><th scope="col">Days</th><th scope="col">Oldest</th></tr></thead>
      <tbody>${folders}${other}</tbody></table>`;
}

/* ─────────────────────────────────────────
   Camera windows
───────────────────────────────────────── */
const _PLAY_ICON = '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M7 4v16l13-8z"/></svg>';
const _EXPAND_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>';

// The idle body of a camera tile: one button that starts the stream.
function _camIdleHTML(host, code, camIp) {
  return `<div class="sa-cam-idle">
    <button type="button" class="sa-btn sa-btn-primary" onclick="toggleCamLive('${escHtml(host)}','${escHtml(code)}')">${_PLAY_ICON}Watch ${escHtml(code)}</button>
    <span class="sa-mono sa-note">${escHtml(camIp)}</span>
  </div>`;
}

function _camStopHTML(host, code) {
  return `<button type="button" class="sa-btn sa-btn-stop" onclick="toggleCamLive('${escHtml(host)}','${escHtml(code)}')"><i aria-hidden="true"></i>Stop</button>`;
}

function mkCameraWindows(host) {
  if (!_canUseLiveFeed(host)) return '';  // live feed requires admin or own-station host
  const cams = state.STATIONS_META[host]?.cameras || [];
  if (!cams.length) return '';
  // Colour-segment cadence, straight off the station's own /api/status. Absent
  // on bundles that predate the field → no health line, never a false alarm.
  const segByCam = (state.statusData[host] || {}).segment_health || {};
  const tiles = cams.map(cam => {
    const key  = `${host}_${cam.code}`;
    const live = !!liveActive[key];
    const seg  = segmentHealthBadge(segByCam[cam.code]);
    const health = seg
      ? `<span class="sa-tone-bad" title="${escHtml(seg.title)}">${escHtml(seg.label[0].toUpperCase() + seg.label.slice(1))}</span>`
      : segByCam[cam.code] ? '<span class="sa-note">Capture steady</span>' : '';
    return `<section class="sa-card sa-cam" aria-labelledby="cam-h-${key}">
      <div class="sa-cam-head">
        <h3 id="cam-h-${key}" class="sa-mono">${escHtml(cam.code)}</h3>${health}
        <span class="sa-cam-ctl" id="cam-ctl-${key}">${live ? _camStopHTML(host, cam.code) : ''}</span>
        <button type="button" class="sa-btn" onclick="expandCam('${escHtml(host)}','${escHtml(cam.code)}','${escHtml(cam.cam_ip)}')"
                aria-label="Full screen ${escHtml(cam.code)}">${_EXPAND_ICON}</button>
      </div>
      <div class="cam-body" id="cam-body-${key}">${live ? _liveBodyHTML(host, cam.code, key) : _camIdleHTML(host, cam.code, cam.cam_ip)}</div>
    </section>`;
  }).join('');
  const note = _isRmsActive(host)
    ? `<p class="sa-note">RMS is capturing, so each stream stops after ${LIVE_VIEW_MAX_S} seconds. Watching a camera can drop RMS frames.</p>` : '';
  return `<div class="sa-stack">${note}<div class="sa-cams">${tiles}</div></div>`;
}


function liveStreamLoaded(key) {
  const el = document.getElementById(`live-status-${key}`);
  if (el) el.style.display = 'none';
  _liveRetries[key] = 0;
}

function _pollLiveImg(key) {
  const img = document.getElementById(`live-img-${key}`);
  if (!img || !liveActive[key]) return;
  if (img.naturalWidth > 0) { liveStreamLoaded(key); return; }
  img.decode()
    .then(() => { if (liveActive[key]) liveStreamLoaded(key); })
    .catch(() => {});
  setTimeout(() => _pollLiveImg(key), 400);
}

function liveStreamError(img, host, camCode, key) {
  if (!liveActive[key]) return;
  _liveRetries[key] = (_liveRetries[key] || 0) + 1;
  img.style.display = 'none';
  const el = document.getElementById(`live-status-${key}`);

  if (_liveRetries[key] > 3) {
    if (el) el.textContent = 'Stream unavailable';
    fetch(`/stream/${host}/${camCode}`, { signal: AbortSignal.timeout(12000) })
      .then(async r => {
        if (r.ok) return null;
        try { return await r.json(); } catch { return null; }
      })
      .then(data => {
        if (!liveActive[key] || !el) return;
        if (data && (data.detail || data.error))
          el.textContent = data.detail || data.error;
        el.classList.add('error');
      })
      .catch(() => { if (el) el.classList.add('error'); });
    return;
  }

  if (el) {
    el.textContent = `Connecting… (attempt ${_liveRetries[key] + 1}/4)`;
    el.style.display = '';
  }
  setTimeout(() => {
    if (!liveActive[key]) return;
    img.style.display = 'block';
    img.src = `/stream/${host}/${camCode}?t=${Date.now()}`;
  }, 5000);
}

function _isRmsActive(host) {
  // "RMS is running" = any rms-cam*.service or rms-capture.service reported
  // active in the latest /api/status payload. Necessary signal that live view
  // could interfere; not a perfect "actively capturing right now" check (the
  // services stay active 24/7), but the cost of an unnecessary 60 s cap is
  // small and the cost of frame-drops during a real capture night is high.
  const svcs = (state.statusData[host] || {}).services || {};
  return Object.entries(svcs).some(([k, v]) =>
    (k.startsWith('rms-cam') || k === 'rms-capture') && serviceIsUp(v)
  );
}

function _clearLiveCountdown(key) {
  const st = liveAutoStop[key];
  if (!st) return;
  if (st.timeoutId)  clearTimeout(st.timeoutId);
  if (st.intervalId) clearInterval(st.intervalId);
  delete liveAutoStop[key];
}

function _startLiveCountdown(host, camCode) {
  const key = `${host}_${camCode}`;
  _clearLiveCountdown(key);
  const deadline = Date.now() + LIVE_VIEW_MAX_S * 1000;
  const tick = () => {
    const el = document.getElementById(`live-cd-${key}`);
    if (!el) { _clearLiveCountdown(key); return; }
    const sLeft = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
    el.textContent = sLeft;
  };
  const timeoutId = setTimeout(() => {
    if (!liveActive[key]) return;
    toggleCamLive(host, camCode);
  }, LIVE_VIEW_MAX_S * 1000);
  const intervalId = setInterval(tick, 1000);
  liveAutoStop[key] = { deadline, timeoutId, intervalId };
  tick();
}

function _liveBodyHTML(host, camCode, key) {
  const warn = _isRmsActive(host);
  // While RMS captures, a countdown: the bar empties as the stream nears its stop.
  const overlay = warn ? `
    <div class="sa-live-warn">
      <div><span><strong>RMS is capturing.</strong> This stream can cost RMS frames.</span>
        <span>Stops in <b class="sa-mono" id="live-cd-${key}">${LIVE_VIEW_MAX_S}</b> s</span></div>
      <i aria-hidden="true" style="animation-duration:${LIVE_VIEW_MAX_S}s"></i>
    </div>` : '';
  _liveRetries[key] = 0;
  setTimeout(() => _pollLiveImg(key), 800);
  return `<div style="position:relative;width:100%;height:100%">
    <img id="live-img-${key}" src="/stream/${host}/${camCode}"
         style="width:100%;height:100%;object-fit:contain;display:block;background:#000"
         onload="liveStreamLoaded('${key}')"
         onerror="liveStreamError(this,'${host}','${camCode}','${key}')"
         alt="${camCode}" decoding="async">
    <div class="live-status" id="live-status-${key}">Connecting&hellip;</div>
    ${overlay}
  </div>`;
}

function toggleCamLive(host, camCode) {
  const key = `${host}_${camCode}`;
  if (liveActive[key]) {
    const img = document.getElementById(`live-img-${key}`);
    if (img) img.src = '';
    liveActive[key] = false;
    delete _liveRetries[key];
    _clearLiveCountdown(key);
  } else {
    liveActive[key] = true;
  }
  const live = liveActive[key];

  const ctl = document.getElementById(`cam-ctl-${key}`);
  if (ctl) ctl.innerHTML = live ? _camStopHTML(host, camCode) : '';
  const body = document.getElementById(`cam-body-${key}`);
  if (body) {
    const cam = (state.STATIONS_META[host]?.cameras || []).find(c => c.code === camCode);
    body.innerHTML = live ? _liveBodyHTML(host, camCode, key) : _camIdleHTML(host, camCode, cam?.cam_ip || '');
    if (live) {
      const img = document.getElementById(`live-img-${key}`);
      if (img) img.addEventListener('load', () => liveStreamLoaded(key));
    }
    // Keep focus on the tile: the button that was pressed is gone.
    (live ? ctl?.querySelector('button') : body.querySelector('button'))?.focus();
  }

  // Start the auto-stop countdown only when RMS is actively running. During
  // daytime / RMS-stopped state the live feed is harmless — no cap.
  if (live && _isRmsActive(host)) _startLiveCountdown(host, camCode);
}

/* ─────────────────────────────────────────
   Modal accessibility — shared helpers
   ─────────────────────────────────────────
   _modalOpen / _modalClose / _MODAL_TABBABLE_SEL / _modalTabbables /
   _modalTrapHandler / _closeAllOpenModals all live in dashboard-common.js
   now (which loads first on every page). All non-module <script> tags
   share a single global-script lexical environment, so calls to those
   helpers from this bundle resolve at runtime as plain identifiers; the
   window._modalOpen / window._modalClose mirrors are there for inline
   <script> blocks in templates that prefer the explicit global form.
───────────────────────────────────────── */

function expandCam(host, camCode, camIp) {
  const modal = document.getElementById('cam-modal');
  const img   = document.getElementById('cam-modal-img');
  const title = document.getElementById('cam-modal-title');
  title.textContent = `${camCode}  ·  ${camIp}  ·  LIVE`;
  const dlBtn = document.getElementById('cam-modal-dl');
  if (dlBtn) dlBtn.style.display = 'none';
  // The shared cam-modal-img is reused across live feed + RMS plot modes;
  // make sure any leftover rotation from a previous plot view is cleared
  // before showing the live feed.
  img.classList.remove('thumb-rotated');
  img.src = `/stream/${host}/${camCode}`;
  _modalOpen(modal);
}

function closeCamModal(e) {
  if (e && e.target !== document.getElementById('cam-modal')) return;
  if (window._pz.justDragged) return;
  const modal = document.getElementById('cam-modal');
  const img   = document.getElementById('cam-modal-img');
  img.src = '';
  img.classList.remove('thumb-rotated');
  if (modal.dataset.plotZoom) {
    img.removeEventListener('wheel',     window._pzWheel);
    img.removeEventListener('mousedown', window._pzDown);
    document.removeEventListener('mousemove', window._pzMove);
    document.removeEventListener('mouseup',   window._pzUp);
    img.style.transform = '';
    img.style.transition = '';
    img.style.cursor = '';
    delete modal.dataset.plotZoom;
    modal.classList.remove('plot-mode');
  }
  _modalClose(modal);
}

function stackOpenModal(station, camera, date, stackFile, time, idx = -1, imgSrc = null) {
  window._stackCurrentIdx = idx;
  window._stackModalCtx   = {station, camera, date};
  const url = imgSrc || `/fullstack/${station}/${camera}/${date}/${encodeURIComponent(stackFile)}`;
  document.getElementById('stack-modal-title').textContent = `${camera}  ${time} UTC`;
  document.getElementById('stack-modal-img').src = url;
  document.getElementById('stack-modal-dl').href = url + (imgSrc ? '' : '?download=1');
  document.getElementById('stack-modal-dl').download = stackFile;
  _modalOpen(document.getElementById('stack-modal'));
}

function stackNavModal(delta) {
  if (!window._vdbVisibleChunks.length || !window._stackModalCtx) return;
  const arrayDir = (window.vdbSortNewest ? -delta : delta) > 0 ? 1 : -1;
  let next = window._stackCurrentIdx + arrayDir;
  while (next >= 0 && next < window._vdbVisibleChunks.length) {
    if (window._vdbVisibleChunks[next].stack) break;
    next += arrayDir;
  }
  if (next < 0 || next >= window._vdbVisibleChunks.length) return;
  const c = window._vdbVisibleChunks[next];
  const {station, camera, date} = window._stackModalCtx;
  const archiveImgSrc = c.source === 'archive'
    ? `/api/archive/file/${camera}/${date}/${c.stack_subdir || 'meteors'}/${encodeURIComponent(c.stack)}`
    : null;
  stackOpenModal(station, camera, date, c.stack, c.time, next, archiveImgSrc);
}

function stackCloseModal(e) {
  if (e && e.target !== document.getElementById('stack-modal')) return;
  document.getElementById('stack-modal-img').src = '';
  _modalClose(document.getElementById('stack-modal'));
}

/* Timelapses arrive with the station's status (state.tlData, archive-served);
   the Final Data Products page shows each camera's for its night. */
function renderTimelapses(host) {
  if (state.activeTab === 'fdp' && state.activeStation === host) _fdpFillTimelapses(host);
}

/* ─────────────────────────────────────────
   Final Data Products tab — nightly outputs only:
   timelapses, nightly sky-dome MP4, per-camera RMS plots, GMN cross-link.
───────────────────────────────────────── */
/* The page's night: the shared night (state.view.date), so a night picked
   here, on the RMS tab or in the Video DB stays picked across tabs and
   stations; else the night that should be processed by now. Every product
   on the page is that night's. */
function _fdpNight(_host) {
  return state.view.date || fdpRecentNights()[0];
}

export function fdpIsoToYmd(iso) {
  return /^\d{4}-\d{2}-\d{2}$/.test(iso || '') ? iso.replace(/-/g, '') : null;
}

export function fdpYmdToIso(ymd) {
  return /^\d{8}$/.test(ymd || '')
    ? `${ymd.slice(0, 4)}-${ymd.slice(4, 6)}-${ymd.slice(6, 8)}` : '';
}

/* The n most recent nights that should be processed by now, newest first:
   yesterday (UTC) backwards, the same expected-night convention as the
   archive's plots-latest resolver. */
export function fdpRecentNights(now = new Date(), n = 3) {
  const out = [];
  for (let i = 1; i <= n; i++) {
    const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - i));
    out.push(d.toISOString().slice(0, 10).replace(/-/g, ''));
  }
  return out;
}

function fdpOnNightChange(host, iso) {
  const night = fdpIsoToYmd(iso);
  if (!night) return;
  state.view.date = night;
  stationSyncUrl();
  renderFinalDataProducts(host);
}

function fdpStepNight(host, delta) {
  const d = _fdpNight(host);
  const t = new Date(Date.UTC(+d.slice(0, 4), +d.slice(4, 6) - 1, +d.slice(6, 8) + delta));
  const next = t.toISOString().slice(0, 10).replace(/-/g, '');
  if (next > fdpRecentNights()[0]) return;
  fdpOnNightChange(host, fdpYmdToIso(next));
}

// Whole days between a night and today (UTC); last night is 1.
function _fdpAgeDays(night) {
  const t = Date.UTC(+night.slice(0, 4), +night.slice(4, 6) - 1, +night.slice(6, 8));
  const now = new Date();
  return Math.round((Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()) - t) / 86400000);
}

// What a missing product means for this night: recent ones are on the way,
// timelapses older than their 14 days are gone, the rest never came.
export function fdpMissingText(kind, ageDays) {
  if (ageDays <= 1) return 'Not uploaded yet';
  if (kind === 'tl' && ageDays > 14) return 'Removed after 14 days';
  return 'None';
}

// One cell of "What this night produced".
function _fdpSetHave(host, cam, kind, text, ok) {
  const cell = document.getElementById(`fdp-have-${host}-${cam}-${kind}`);
  if (!cell) return;
  cell.textContent = text;
  cell.className = ok ? 'fdp-yes' : 'fdp-no';
}

/* <img> load/error of an image tile: the tile reports to the grid; a missing
   image becomes a plain line rather than a broken image. */
function _fdpTileLoaded(img) {
  const t = img.closest('[data-kind]');
  if (t) _fdpSetHave(t.dataset.host, t.dataset.cam, t.dataset.kind, 'Yes', true);
}

function _fdpTileMissing(img) {
  const t = img.closest('[data-kind]');
  if (!t) return;
  const text = fdpMissingText(t.dataset.kind, _fdpAgeDays(t.dataset.night));
  t.querySelector('.fdp-open')?.replaceWith(Object.assign(document.createElement('div'),
    { className: 'fdp-empty', textContent: text }));
  t.querySelector('.fdp-dl')?.remove();
  _fdpSetHave(t.dataset.host, t.dataset.cam, t.dataset.kind, text, false);
}

const _FDP_DL_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4v12M6 11l6 6 6-6M5 20h14"/></svg>';
const _FDP_PLAY = '<span class="fdp-play" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M7 4v16l13-8z"/></svg></span>';

/* ─────────────────────────────────────────
   Video DB tab — live BW sky-dome card.
   Separate variant (?variant=ff_max) so it always shows tonight's newest
   FF maxpixel coverage, regardless of which date the user picked in the
   VDB filters below. Refreshed every 60 s while the tab is active.
───────────────────────────────────────── */
function mkVdbSkyDome(host) {
  const bust = Date.now();
  return `<div class="card sky-dome-card" id="vdb-sky-dome-card-${host}" style="padding:10px 14px;margin-bottom:12px">
    <div class="card-title" style="margin-bottom:6px">
      Sky right now
      <span style="font-size:10px;color:var(--muted);font-weight:400;margin-left:8px">
        live - refreshes every ~90 seconds - always tonight, regardless of date selector below
      </span>
    </div>
    <div class="sky-dome-wrap">
      <img class="sky-dome-img" id="vdb-sky-dome-img-${host}"
           src="/api/sky_dome/${encodeURIComponent(host)}.png?variant=ff_max&t=${bust}"
           alt="Sky right now for ${host}"
           loading="lazy" decoding="async"
           onerror="vdbSkyDomeOnError('${host}')"
           role="button" tabindex="0"
           onclick="vdbSkyDomeOpenModal('${host}')">
    </div>
    ${domeFreshLabelHtml(host, 'vdb')}
  </div>`;
}

function vdbMountSkyDome(host) {
  const pane = document.getElementById('pane-videodb');
  if (!pane) return;
  pane.querySelectorAll('[id^="vdb-sky-dome-card-"]').forEach(node => {
    if (node.id !== `vdb-sky-dome-card-${host}`) node.remove();
  });
  if (document.getElementById(`vdb-sky-dome-card-${host}`)) return;
  const wrap = document.createElement('div');
  wrap.innerHTML = mkVdbSkyDome(host);
  pane.insertBefore(wrap.firstElementChild, pane.firstElementChild);
}

function vdbRefreshSkyDome(host) {
  const img = document.getElementById(`vdb-sky-dome-img-${host}`);
  if (!img) return;
  const card = document.getElementById(`vdb-sky-dome-card-${host}`);
  if (card) card.style.display = '';
  img.src = `/api/sky_dome/${encodeURIComponent(host)}.png?variant=ff_max&t=${Date.now()}`;
  domeFetchFreshness(host);
}

/* ─────────────────────────────────────────
   Sky-dome freshness indicator — shared between the Video DB "Sky right
   now" card and the RMS / Detection "Sky coverage" card. Reads the
   server-side sidecar JSON to learn the newest FF capture time across
   cameras the last render used; colours the label orange > 5 min,
   red > 10 min behind wall-clock. "Now" updates every second.
───────────────────────────────────────── */
function domeFreshLabelHtml(host, side) {
  // side === 'vdb' or 'rms' — two surfaces, identical layout, distinct IDs
  // so we can update whichever is currently mounted.
  return `<div class="sky-dome-fresh" id="${side}-sky-dome-fresh-${host}"
              style="font-size:11px;color:var(--muted);margin-top:8px;display:flex;gap:18px;justify-content:center;flex-wrap:wrap;font-family:'SF Mono',Consolas,monospace">
    <span><span style="color:var(--muted);margin-right:6px">Image:</span><span class="sky-dome-fresh-img">&mdash;</span></span>
    <span style="display:none"><span class="sky-dome-fresh-lag"></span></span>
    <span><span style="color:var(--muted);margin-right:6px">Now:</span><span class="sky-dome-fresh-now">&mdash;</span></span>
  </div>`;
}

export function _domeFmtUTC(ms) {
  return new Date(ms).toISOString().slice(11, 19) + ' UTC';
}

/* Compact "time since" formatter for the live-image lag indicator.
   < 60 s   -> "just now"
   < 60 min -> "12m ago"
   < 24 h   -> "3h 14m ago"
   < 30 d   -> "2d 5h ago"
   >= 30 d  -> "47d ago" (no further granularity needed at that scale) */
export function _domeFmtRelative(ageMs) {
  if (!Number.isFinite(ageMs) || ageMs < 0) return '';
  const s = Math.floor(ageMs / 1000);
  if (s < 60) return 'just now';
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  const mRem = m % 60;
  if (h < 24) return mRem ? `${h}h ${mRem}m ago` : `${h}h ago`;
  const d = Math.floor(h / 24);
  const hRem = h % 24;
  if (d < 30) return hRem ? `${d}d ${hRem}h ago` : `${d}d ago`;
  return `${d}d ago`;
}

async function domeFetchFreshness(host) {
  try {
    const r = await fetch(`/api/sky_dome_meta/${encodeURIComponent(host)}?variant=ff_max`);
    if (!r.ok) { _domeFreshness[host] = null; }
    else {
      const d = await r.json();
      _domeFreshness[host] = (d && d.capture_time)
        ? { captureMs: new Date(d.capture_time).getTime() }
        : null;
    }
  } catch (e) { _domeFreshness[host] = null; }
  domeUpdateFreshnessLabels(host);
}

function domeUpdateFreshnessLabels(host) {
  const now = Date.now();
  const state = _domeFreshness[host];
  let imgText, imgColor, lagText = '';
  const haveCapture = !!(state && Number.isFinite(state.captureMs));
  if (haveCapture) {
    imgText = _domeFmtUTC(state.captureMs);
    const ageMs = Math.max(0, now - state.captureMs);
    const ageS = ageMs / 1000;
    if (ageS > 600) imgColor = '#f85149';        // > 10 min behind: red
    else if (ageS > 300) imgColor = '#f5a623';   // > 5 min behind: orange
    else imgColor = 'var(--text,#e6e6e6)';
    lagText = _domeFmtRelative(ageMs);
  } else {
    imgText = 'unknown';
    imgColor = 'var(--muted)';
  }
  const nowText = _domeFmtUTC(now);
  for (const side of ['vdb', 'rms']) {
    const wrap = document.getElementById(`${side}-sky-dome-fresh-${host}`);
    if (!wrap) continue;
    const imgEl = wrap.querySelector('.sky-dome-fresh-img');
    const nowEl = wrap.querySelector('.sky-dome-fresh-now');
    const lagEl = wrap.querySelector('.sky-dome-fresh-lag');
    if (imgEl) { imgEl.textContent = imgText; imgEl.style.color = imgColor; }
    if (nowEl) {
      nowEl.textContent = nowText;
      // Hide the "Now: HH:MM" segment when we have no capture time — showing
      // a live-ticking clock next to "Image: unknown" reads as "data refresh
      // is working" which is misleading.
      const nowParent = nowEl.parentElement;
      if (nowParent) nowParent.style.display = haveCapture ? '' : 'none';
    }
    if (lagEl) {
      lagEl.textContent = lagText;
      lagEl.style.color = imgColor;
      // Hide entirely (incl. the leading "(") when we have nothing to say —
      // avoids a stray "()" on the unknown-image rendering.
      const parent = lagEl.parentElement;
      if (parent) parent.style.display = lagText ? '' : 'none';
    }
  }
}

function domeEnsureFreshTicker() {
  if (_domeFreshTicker) clearInterval(_domeFreshTicker);
  _domeFreshTicker = setInterval(() => {
    if (state.activeStation) domeUpdateFreshnessLabels(state.activeStation);
  }, 1000);
}

// Browsers throttle setInterval in background tabs (sometimes to a single
// fire per minute, often much slower). Without this, a user who tabs away
// for 5 minutes returns to a sky-dome that is several minutes stale and
// has to wait up to another 60 s for the next interval tick. Fire a
// catch-up refresh + freshness fetch the instant the tab becomes visible
// again while we're on the Video DB pane.
if (typeof document !== 'undefined' && !window._domeVisHookInstalled) {
  window._domeVisHookInstalled = true;
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') return;
    if (state.activeTab !== 'videodb' || !state.activeStation) return;
    vdbRefreshSkyDome(state.activeStation);
  });
}

function vdbSkyDomeOnError(host) {
  const card = document.getElementById(`vdb-sky-dome-card-${host}`);
  if (card) card.style.display = 'none';
}

function vdbSkyDomeOpenModal(host) {
  const img = document.getElementById(`vdb-sky-dome-img-${host}`);
  if (!img || !img.src) return;
  const modal = document.getElementById('cam-modal');
  if (modal && typeof rmsPlotExpand === 'function') {
    rmsPlotExpand(modal, img.src, `${host} — sky right now`);
    return;
  }
  window.open(img.src, '_blank', 'noopener');
}

const NIGHT_COMPOSITE_FN = '__night_composite__.webp';
const COLOR_STACK_FN = '__color_meteor_stack__.webp';

function _fdpPointing(az, alt) {
  if (az == null) return '';
  const dirs = ['north', 'north-east', 'east', 'south-east', 'south', 'south-west', 'west', 'north-west'];
  const dir = dirs[Math.round(((az % 360) + 360) % 360 / 45) % 8];
  return `looking ${dir}${alt != null ? `, ${Math.round(alt)}° above the horizon` : ''}`;
}

function renderFinalDataProducts(host) {
  const el = document.getElementById('pane-fdp');
  if (!el) return;
  if (!canAccessTab('fdp', host)) {
    el.innerHTML = window._accessBanner('Final Data Products');
    return;
  }
  const night = _fdpNight(host);
  const cams = state.VDB_CAMERAS[host] || [];
  const meta = Object.fromEntries((state.STATIONS_META[host]?.cameras || []).map(c => [c.code, c]));
  const title = nightTitle(night);
  const latest = night >= fdpRecentNights()[0];
  const h = escHtml(host);

  const kinds = [['tl', 'Timelapse'], ['comp', 'Night composite'], ['stack', 'Meteor stack'], ['plots', 'RMS plots']];
  const haveRows = cams.map(cam => `<tr><th scope="row" class="sa-mono">${escHtml(cam)}</th>${kinds.map(([k]) =>
    `<td id="fdp-have-${h}-${escHtml(cam)}-${k}" class="fdp-wait">Checking…</td>`).join('')}</tr>`).join('');

  const tile = (cam, kind, label, sub, url) => `<div class="fdp-tile" data-host="${h}" data-cam="${escHtml(cam)}" data-kind="${kind}" data-night="${night}">
      <button type="button" class="fdp-open fdp-plot-tile-trigger" data-img-url="${escHtml(url)}" data-label="${escHtml(cam)}: ${label}"
              aria-label="Enlarge ${escHtml(cam)}'s ${label.toLowerCase()}">
        <img src="${escHtml(url)}" loading="lazy" decoding="async" alt="" onload="_fdpTileLoaded(this)" onerror="_fdpTileMissing(this)">
      </button>
      <div class="fdp-cap"><span class="sa-strong">${label}</span><span class="sa-note">${sub}</span>
        <a class="fdp-dl" href="${escHtml(url)}" download aria-label="Download ${escHtml(cam)}'s ${label.toLowerCase()}">${_FDP_DL_ICON}</a></div>
    </div>`;

  const camSections = cams.map(cam => {
    const apiHost = window.hostForCamera(host, cam);
    const img = fn => `/api/rms/plot_image/${apiHost}/${cam}/${night}/${encodeURIComponent(fn)}`;
    const c = escHtml(cam);
    return `<section class="sa-card fdp-cam" id="fdp-cam-${h}-${c}" aria-labelledby="fdp-cam-h-${h}-${c}">
      <div class="fdp-cam-head"><h2 id="fdp-cam-h-${h}-${c}" class="sa-mono">${c}</h2>
        <span class="sa-note">${escHtml(_fdpPointing(meta[cam]?.az, meta[cam]?.alt))}</span></div>
      <div class="fdp-tiles">
        <div class="fdp-tile" id="fdp-tl-${h}-${c}"><div class="fdp-empty">Loading…</div>
          <div class="fdp-cap"><span class="sa-strong">Timelapse</span></div></div>
        ${tile(cam, 'comp', 'Night composite', 'every frame stacked', img(NIGHT_COMPOSITE_FN))}
        ${tile(cam, 'stack', 'Meteor stack', 'every meteor of the night', img(COLOR_STACK_FN))}
      </div>
      <div id="fdp-plots-${h}-${c}"></div>
    </section>`;
  }).join('');

  el.innerHTML = `<div class="fdp-root">
    <div class="fdp-head">
      <div class="sa-stack"><h1>${title}</h1>
        <p class="sa-note">Everything the station made from this night. Timelapses are kept for 14 days and the sky dome for a limited time; composites, meteor stacks and RMS plots stay.</p></div>
      <div class="fdp-nav">
        <button type="button" class="sa-btn dt-tall" aria-label="Previous night" onclick="fdpStepNight('${h}',-1)">‹</button>
        <input type="date" id="fdp-night-input-${h}" class="sa-field dt-tall sa-mono" aria-label="Night" max="${fdpYmdToIso(fdpRecentNights()[0])}"
               value="${fdpYmdToIso(night)}" onchange="fdpOnNightChange('${h}', this.value)">
        <button type="button" class="sa-btn dt-tall" aria-label="Next night" onclick="fdpStepNight('${h}',1)" ${latest ? 'disabled' : ''}>›</button>
      </div>
    </div>
    <section class="sa-card fdp-hero" aria-label="The whole sky and what this night produced">
      <div class="fdp-dome-col">
        <div class="fdp-dome" id="fdp-dome-${h}"><span class="sa-note">Loading…</span></div>
        <div class="fdp-dome-cap"><h2>The whole sky</h2><span class="sa-note">every camera merged, one frame a minute</span></div>
      </div>
      <div class="fdp-have-col">
        <h2>What this night produced</h2>
        <div class="fdp-have-scroll"><table class="sa-table fdp-have">
          <thead><tr><th scope="col">Camera</th>${kinds.map(([, l]) => `<th scope="col">${l}</th>`).join('')}</tr></thead>
          <tbody>${haveRows}</tbody></table></div>
        <p class="sa-note">A gap means the product never reached the archive; Station Admin shows where that night's pipeline stopped.</p>
      </div>
    </section>
    ${camSections}
  </div>`;
  el.onclick = _fdpPlotTileClick;

  fdpLoadDome(host, night);
  cams.forEach(cam => fdpLoadCamPlots(host, cam, night));
  _fdpFillTimelapses(host);
}

/* Each camera's timelapse for the night, from state.tlData (timelapses come
   with the station's status, kept 14 days). The length shows once the
   video's metadata has loaded. */
function _fdpFillTimelapses(host) {
  const night = _fdpNight(host);
  const tl = state.tlData[host];
  if (!tl) return;  // renderTimelapses fills in once they arrive
  const allowed = canAccessTab('timelapse', host);
  for (const cam of state.VDB_CAMERAS[host] || []) {
    const box = document.getElementById(`fdp-tl-${escHtml(host)}-${escHtml(cam)}`);
    if (!box || box.dataset.night === night) continue;
    box.dataset.night = night;
    const e = (tl[cam] || []).find(x => x.date === night);
    if (!allowed || !e) {
      const text = !allowed ? 'Not public for this station' : fdpMissingText('tl', _fdpAgeDays(night));
      box.innerHTML = `<div class="fdp-empty">${text}</div><div class="fdp-cap"><span class="sa-strong">Timelapse</span></div>`;
      _fdpSetHave(host, cam, 'tl', allowed ? text : 'Not public', false);
      continue;
    }
    const arc = e.source === 'archive';
    const src = arc ? `/api/archive/file/${cam}/${e.date}/timelapse/${e.filename}` : `/timelapse/${host}/${cam}/${e.date}/${e.filename}`;
    const poster = e.night_stack ? (arc ? `/api/archive/file/${cam}/${e.date}/timelapse/${e.night_stack}` : `/night_stack/${host}/${cam}/${e.date}/${e.night_stack}`) : '';
    const dl = arc ? src : `/timelapse_download/${host}/${cam}/${e.date}/${e.filename}`;
    box.innerHTML = `<video class="fdp-video" src="${escHtml(src)}" ${poster ? `poster="${escHtml(poster)}"` : ''} controls preload="metadata"
        aria-label="${escHtml(cam)}'s timelapse" onloadedmetadata="this.nextElementSibling.querySelector('.fdp-len').textContent = _fdpLen(this.duration)"></video>
      <div class="fdp-cap"><span class="sa-strong">Timelapse</span><span class="sa-note sa-mono fdp-len"></span>
        <a class="fdp-dl" href="${escHtml(dl)}" download aria-label="Download ${escHtml(cam)}'s timelapse">${_FDP_DL_ICON}</a></div>`;
    _fdpSetHave(host, cam, 'tl', 'Yes', true);
  }
}

function _fdpLen(sec) {
  if (!isFinite(sec)) return '';
  return `${Math.floor(sec / 60)}:${String(Math.round(sec % 60)).padStart(2, '0')}`;
}

/* The night's sky dome: one MP4 per night, round like the sky. */
async function fdpLoadDome(host, night) {
  const box = document.getElementById(`fdp-dome-${escHtml(host)}`);
  if (!box) return;
  let dates = [];
  try {
    const r = await fetch(`/api/sky_dome_timelapse/${encodeURIComponent(host)}/dates`);
    if (r.ok) dates = (await r.json())?.dates || [];
  } catch (e) { /* shown as absent */ }
  if (!dates.includes(night)) {
    box.innerHTML = `<span class="sa-note">${_fdpAgeDays(night) <= 1 ? 'The sky dome appears after dawn.' : 'No sky dome for this night.'}</span>`;
    return;
  }
  const url = `/api/sky_dome_timelapse/${encodeURIComponent(host)}/${encodeURIComponent(night)}.mp4`;
  box.innerHTML = `<video src="${url}" preload="metadata" playsinline aria-label="The whole sky through the night"
      onclick="this.paused ? this.play() : this.pause()" onplay="this.nextElementSibling.hidden = true" onpause="this.nextElementSibling.hidden = false"></video>
    <button type="button" class="fdp-dome-play" aria-label="Play the sky dome of the night" onclick="this.previousElementSibling.play()">${_FDP_PLAY}</button>`;
}

/* A camera's RMS plots for the night, folded into one row. */
async function fdpLoadCamPlots(host, cam, night) {
  const box = document.getElementById(`fdp-plots-${escHtml(host)}-${escHtml(cam)}`);
  if (!box) return;
  const apiHost = window.hostForCamera(host, cam);
  let plots = [];
  try {
    const r = await fetch(`/api/rms/plots-latest/${apiHost}/${cam}?date=${encodeURIComponent(night)}`);
    if (r.ok) {
      const res = await r.json();
      if (res && res.date === night && Array.isArray(res.plots)) plots = res.plots.filter(p => p && p.filename);
    }
  } catch (e) { /* shown as absent */ }
  if (!document.body.contains(box)) return;
  if (!plots.length) {
    const text = fdpMissingText('plots', _fdpAgeDays(night));
    box.innerHTML = `<div class="fdp-plots-none">RMS plots <span class="sa-note">${text}</span></div>`;
    _fdpSetHave(host, cam, 'plots', text, false);
    return;
  }
  const tiles = plots.map(p => {
    const url = `/api/rms/plot_image/${apiHost}/${cam}/${night}/${encodeURIComponent(p.filename)}`;
    const label = p.label || p.filename;
    return `<button type="button" class="fdp-plot fdp-plot-tile-trigger" data-img-url="${escHtml(url)}" data-label="${escHtml(label)}">
      <img src="${escHtml(url)}" loading="lazy" decoding="async" alt="" onerror="this.closest('.fdp-plot').remove()">
      <span>${escHtml(label)}</span></button>`;
  }).join('');
  box.innerHTML = `<details class="dt-plots fdp-plots"><summary><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>
      RMS plots <span class="sa-note">${plots.length}</span></summary>
    <div class="fdp-plot-grid">${tiles}</div></details>`;
  _fdpSetHave(host, cam, 'plots', `${plots.length} plot${plots.length !== 1 ? 's' : ''}`, true);
}

function _fdpPlotTileClick(e) {
  const tile = e.target.closest('.fdp-plot-tile-trigger');
  if (!tile) return;
  fdpOpenRmsPlot(tile.dataset.imgUrl, tile.dataset.label);
}

/* Click-to-enlarge for FDP RMS night plot thumbs. Prefers the shared
   cam-modal (same path RMS / Detection uses via rmsPlotExpand); falls
   back to opening the image URL in a new tab if the modal DOM isn't on
   this template. */
function fdpOpenRmsPlot(imgUrl, label) {
  const modal = document.getElementById('cam-modal');
  const img   = document.getElementById('cam-modal-img');
  const title = document.getElementById('cam-modal-title');
  if (modal && img && title && typeof rmsPlotExpand === 'function') {
    rmsPlotExpand(modal, imgUrl, label);
    return;
  }
  window.open(imgUrl, '_blank', 'noopener');
}



/* ─────────────────────────────────────────
   Data fetching
───────────────────────────────────────── */
// "Updated HH:MM:SS UTC" in the station toolbar, from the active station's
// last status (received_at is the dashboard's clock, immune to station skew).
const _fmtUpdatedUTC = new Intl.DateTimeFormat('en-GB',
  { timeZone: 'UTC', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
function paintLastUpdated() {
  const el = document.getElementById('ts');
  if (!el) return;
  const d = state.statusData[state.activeStation];
  const raw = d && (d.received_at || d.last_updated);
  const t = raw ? new Date(raw) : null;
  el.textContent = t && !isNaN(t) ? `Updated ${_fmtUpdatedUTC.format(t)} UTC` : '\u2014';
}

async function fetchStatus(host) {
  try {
    const resp = await fetch(`/api/status/${host}`);
    if (!resp.ok) return;
    const data = await resp.json();
    if (!data) return;
    state.statusData[host] = data;
    if (window.updateStationDots) updateStationDots();
    if (state.activeStation === host) paintLastUpdated();
    if (state.activeStation === host && state.activeTab === 'settings') {
      const secSA = document.getElementById(`sec-services-sysadmin-${host}`);
      if (secSA) secSA.innerHTML = mkServices(data.services || {});
      const stEl = document.getElementById(`storage-data-${host}`);
      if (stEl) stEl.innerHTML = mkStorage(data.storage, data.disk, data.extra_disks, data.disk_pressure, data.capture_fallback, window.settingsData[host]?.disk);
      // Colour capture turning on or off changes the banner and the
      // Configuration card: it is part of the render signature, so a
      // re-render rebuilds just then.
      window.renderSysAdminIfChanged?.(host);
    }
    // Fetch timelapses lazily
    if (!state.tlData[host]) fetchTimelapses(host);
  } catch(e) { console.warn('fetchStatus failed', e); }
}

async function fetchVitals(host) {
  try {
    const resp = await fetch(`/api/vitals/${host}`);
    if (!resp.ok) return;
    const data = await resp.json();
    if (!data) return;
    state.vitalsData[host] = data;
    _pushVitalsHistory(host, data);
  } catch(e) { console.warn('fetchVitals failed', e); }
}

// One round-trip for all stations' status. Cuts N × RTT (where N = number of
// stations) plus N TCP/TLS connection setups on every poll cycle — the
// difference is most visible from high-latency clients (Romania → Falkenstein
// is ~70 ms, so 8 stations = 560 ms shaved per refresh).
async function fetchAllStatus() {
  try {
    const resp = await fetch('/api/status/all');
    if (!resp.ok) return;
    const all = await resp.json();
    for (const [host, data] of Object.entries(all || {})) {
      if (!data || Object.keys(data).length === 0) continue;
      state.statusData[host] = data;
    }
    if (window.updateStationDots) updateStationDots();
    paintLastUpdated();
  } catch(e) { console.warn('fetchAllStatus failed', e); }
}

async function fetchAllVitals() {
  try {
    const resp = await fetch('/api/vitals/all');
    if (!resp.ok) return;
    const all = await resp.json();
    for (const [host, data] of Object.entries(all || {})) {
      if (!data || Object.keys(data).length === 0) continue;
      state.vitalsData[host] = data;
      _pushVitalsHistory(host, data);
    }
  } catch(e) { console.warn('fetchAllVitals failed', e); }
}

async function fetchStorageForSysAdmin(host) {
  try {
    const resp = await fetch(`/api/status/${host}`);
    if (!resp.ok) return;
    const data = await resp.json();
    if (!data) return;
    state.statusData[host] = data;
    const el = document.getElementById(`storage-data-${host}`);
    if (el) el.innerHTML = mkStorage(data.storage, data.disk, data.extra_disks, data.disk_pressure, data.capture_fallback, window.settingsData[host]?.disk);
    window.fetchStoragewatchStatus(host);
  } catch(e) { /* silent */ }
}

async function fetchVitalsFast(host) {
  try {
    const resp = await fetch(`/api/vitals/${host}`);
    if (!resp.ok) return;
    const data = await resp.json();
    if (!data) return;
    state.vitalsData[host] = data;
    _pushVitalsHistory(host, data);
    if (state.activeStation === host && state.activeTab === 'settings') {
      window.drawAllGraphs(host);
    }
  } catch(e) { /* silent */ }
}

function _pushVitalsHistory(host, data) {
  if (!window.vitalsHistory[host]) window.vitalsHistory[host] = [];
  window.vitalsHistory[host].push({
    cpu_pct:         data.cpu_pct         ?? null,
    temp_c:          data.temp_c          ?? null,
    ram_pct:         data.ram_pct         ?? null,
    disk_write_mbps: data.disk_write_mbps ?? null,
  });
  if (window.vitalsHistory[host].length > 150) window.vitalsHistory[host].shift();
  if (data.cores?.length) window.coreData[host] = {
    cores:       data.cores,
    top_procs:   data.top_procs   ?? null,
    total_cores: data.total_cores ?? null,
  };
}

async function fetchTimelapses(host) {
  try {
    const resp = await fetchOnce(`tab:timelapses:${host}`, `/api/timelapses/${host}`);
    if (!resp.ok) return;
    state.tlData[host] = await resp.json();
    if (state.activeStation === host && state.activeTab === 'fdp') {
      renderTimelapses(host);
    }
  } catch(e) {
    if (_isAbort(e)) return;
    console.warn('fetchTimelapses failed', e);
  }
}

async function fetchRMSRefresh(host) {
  // On manual refresh: re-fetch the "All nights" view per camera. The legacy
  // fetchRMSNights / rmsNightSelected pair (single-night drill-down) was removed
  // when the tab moved to window.fetchRMSAllNights; calling them threw ReferenceError
  // and aborted the rest of refreshCurrent().
  const cameras = state.VDB_CAMERAS[host] || [];
  for (const cam of cameras) {
    await window.fetchRMSAllNights(host, cam);
  }
}

async function fetchSettings(host) {
  document.getElementById('pane-settings').innerHTML =
    '<div class="offline">Loading config.json…</div>';
  const [settingsResp, statsResp, hwResp] = await Promise.allSettled([
    fetchOnce(`tab:settings:${host}`,    `/api/settings/${host}`),
    fetchOnce(`tab:encstats:${host}`,    `/api/encoding/stats/${host}`),
    fetchOnce(`tab:hardware:${host}`,    `/api/hardware/${host}`),
  ]);
  // An abort on any of the three is a tab-switch — bail without mutating state.
  if ([settingsResp, statsResp, hwResp].some(
        s => s.status === 'rejected' && _isAbort(s.reason))) {
    return;
  }
  try {
    const r = settingsResp.status === 'fulfilled' ? settingsResp.value : null;
    if (!r || !r.ok) {
      window.settingsData[host] = {__error: r ? 'HTTP '+r.status+(r.status===404?' (mount missing?)':'') : 'fetch failed'};
    } else {
      window.settingsData[host] = await r.json();
    }
  } catch(e) { window.settingsData[host] = {__error: String(e)}; }
  try {
    const r = statsResp.status === 'fulfilled' ? statsResp.value : null;
    if (r && r.ok) window.encStatsData[host] = await r.json();
  } catch(e) {}
  try {
    const r = hwResp.status === 'fulfilled' ? hwResp.value : null;
    if (r && r.ok) window.hwData[host] = await r.json();
  } catch(e) {}
  if (state.activeStation === host && state.activeTab === 'settings') {
    window.renderSysAdmin?.(host);
  }
}

async function refreshCurrent() {
  const btn  = document.getElementById('refresh-btn');
  const host = state.activeStation;
  btn.textContent = 'Refreshing…';
  btn.disabled    = true;
  try {
    await fetchStatus(host);
    await fetchVitals(host);
    await fetchTimelapses(host);
    if (state.activeTab === 'rms')      await fetchRMSRefresh(host);
    if (state.activeTab === 'settings') await fetchSettings(host);
  } finally {
    btn.textContent = 'Refresh';
    btn.disabled    = false;
  }
}

/* ─────────────────────────────────────────
   Auto-refresh timers
───────────────────────────────────────── */
// Skip polls when the tab isn't visible — saves bandwidth + battery and
// shrinks the noise on the access log. The next visibilitychange event
// triggers a single catch-up refresh in the IIFE bootstrap.
function _docVisible() { return !(document.hidden); }

/* ─────────────────────────────────────────
   Server-Sent Events: status stream
───────────────────────────────────────── */
// Single shared connection — closing+reopening on visibility/reconnect keeps
// this strictly one-per-tab. Tracked at module scope so subscribe/unsubscribe
// helpers can find each other without globals on window.
let _statusES         = null;   // active EventSource, or null
let _statusESLastMsg  = 0;      // performance.now() of last received event
let _statusESDownSince = 0;     // ms timestamp the connection first went bad

// Open one SSE connection. Idempotent: closes any prior one before opening
// a new, so callers (reconnect handler, visibility-resume, bootstrap) don't
// have to bookkeep.
function subscribeToStatusStream() {
  if (!('EventSource' in window)) return false;  // caller will fall back to polling
  closeStatusStream();
  let es;
  try {
    es = new EventSource('/api/events/status');
  } catch (e) {
    console.warn('EventSource open failed', e);
    return false;
  }
  _statusES = es;
  _statusESLastMsg = performance.now();
  _statusESDownSince = 0;

  // Snapshot event: full state.statusData replacement on (re)connect. Mirrors what
  // fetchAllStatus() did at the top of every poll cycle — same downstream
  // call into updateStationDots() so the rest of the UI is none the wiser.
  es.addEventListener('snapshot', (ev) => {
    _statusESLastMsg = performance.now();
    try {
      const all = JSON.parse(ev.data);
      for (const [host, data] of Object.entries(all || {})) {
        if (!data || Object.keys(data).length === 0) continue;
        state.statusData[host] = data;
      }
      if (window.updateStationDots) updateStationDots();
    } catch (e) { console.warn('SSE snapshot parse failed', e); }
  });

  // Incremental status event: one host changed.
  es.addEventListener('status', (ev) => {
    _statusESLastMsg = performance.now();
    try {
      const msg = JSON.parse(ev.data);
      if (msg && msg.host && msg.status) {
        state.statusData[msg.host] = msg.status;
        if (window.updateStationDots) updateStationDots();
      }
    } catch (e) { console.warn('SSE status parse failed', e); }
  });

  // EventSource auto-reconnects on transport errors with its own backoff
  // (3 s default in browsers), so we don't manually re-open here. We DO
  // track when it went down so the watchdog below can trigger a one-shot
  // fetchAllStatus() catch-up if the outage stretches past 2 minutes.
  es.addEventListener('error', () => {
    if (!_statusESDownSince) _statusESDownSince = Date.now();
    // ReadyState 2 (CLOSED) is terminal — browser gave up. Anything else is
    // a transient blip that EventSource is already retrying.
    if (es.readyState === 2) {
      console.warn('SSE connection closed by browser; will be reopened on visibility');
      _statusES = null;
    }
  });
  return true;
}

function closeStatusStream() {
  if (_statusES) {
    try { _statusES.close(); } catch (e) {}
    _statusES = null;
  }
}

// Watchdog: if we haven't heard anything for >2 minutes (heartbeats arrive
// every ~25 s, real events more often), the stream is probably dead behind
// a stale proxy/NAT entry even though EventSource still thinks it's open.
// Force a fetchAllStatus() to catch up and reopen the stream.
function _statusStreamWatchdog() {
  if (!_docVisible() || !_statusES) return;
  const since = performance.now() - _statusESLastMsg;
  if (since > 120000) {
    console.warn('SSE silent for', Math.round(since/1000), 's — forcing reconnect');
    fetchAllStatus();
    subscribeToStatusStream();
  }
}

function startTimers() {
  // Status: prefer SSE push. Polling on /api/status/all is replaced by a
  // server stream; we keep fetchAllStatus() callable for refresh-button +
  // visibility-resume catch-up + watchdog fallback.
  const sseOk = subscribeToStatusStream();
  if (!sseOk) {
    // Browsers without EventSource (very rare — IE/old Edge only) fall back
    // to the previous 60 s aggregated poll. Same code path as before.
    setInterval(() => {
      if (_docVisible()) fetchAllStatus();
    }, 60000);
  } else {
    // Cheap (per-30s) watchdog tick to catch zombie connections.
    setInterval(_statusStreamWatchdog, 30000);
  }
  // Vitals: aggregated, every 60s (was 30s — CPU/temp don't change that fast,
  // and the saved RTT per cycle matters more than a 30 s data freshness gap).
  // SSE intentionally NOT used here — vitals change every second, the delta
  // savings wouldn't beat polling and would add a second long-lived thread on
  // the server with little benefit.
  setInterval(() => {
    if (_docVisible()) fetchAllVitals();
  }, 60000);
}

// Catch up immediately when the tab regains focus instead of waiting for the
// next interval tick. Triggers every paused background poller so the user
// doesn't stare at up-to-two-minutes-stale data right after switching back.
document.addEventListener('visibilitychange', () => {
  if (typeof state.STATIONS_META === 'undefined' || !state.STATIONS_META) return;
  if (!_docVisible()) {
    // Close the SSE proactively — browsers don't always tear down EventSource
    // on tab-hide, and we don't want a long-lived idle TCP connection per
    // tab held just to be courteous when the user comes back.
    closeStatusStream();
    return;
  }
  // Resume: kick the catch-up fetch + reopen the stream.
  fetchAllStatus();
  fetchAllVitals();
  if ('EventSource' in window) subscribeToStatusStream();
  if (typeof window.vdbPoll === 'function') {
    try { window.vdbPoll(); } catch (e) { /* not on VideoDB tab — fine */ }
  }
});





/* ─────────────────────────────────────────
   Access control helpers
───────────────────────────────────────── */
function _ownsStation(host) {
  return state.USER_ROLE === 'admin' || (state.USER_ROLE === 'host' && state.USER_STATIONS.includes(host));
}

function _canUseLiveFeed(host) {
  return _ownsStation(host);
}

function canAccessTab(tab, host) {
  if (state.USER_ROLE === 'admin') return true;
  if (tab === 'settings') return _ownsStation(host);
  if (tab === 'rms') return true;
  if (tab === 'fdp' || tab === 'spectro') {
    if (state.USER_ROLE === 'host' || state.USER_ROLE === 'visitor') return true;
    const meta = state.STATIONS_META[host];
    return meta && Array.isArray(meta.public_tabs)
      && (meta.public_tabs.includes('timelapse') || meta.public_tabs.includes('fdp')
          || meta.public_tabs.includes(tab));
  }
  if (state.USER_ROLE === 'host' || state.USER_ROLE === 'visitor') return true;
  // guest
  const meta = state.STATIONS_META[host];
  return meta && Array.isArray(meta.public_tabs) && meta.public_tabs.includes(tab);
}

// Returns a "not public" banner card for content the current user can't access.
function _accessBanner(title) {
  const msg = state.USER_ROLE === 'guest'
    ? 'The station host has not made this content public.'
    : 'This content is not shared with other hosts.';
  return `<div class="card"><div class="card-title">${title}</div>
    <div style="padding:24px 16px;color:var(--muted);font-size:12px;text-align:center;font-style:italic">${msg}</div>
  </div>`;
}

function _hasSpectroCamera(host) {
  const meta = state.STATIONS_META[host];
  return !!(meta && Array.isArray(meta.cameras)
            && meta.cameras.some(c => c && c.spectro));
}

function updateTabVisibility(host) {
  // videodb tab is always visible — content inside is gated per-card via window._accessBanner
  const tabs = ['rms', 'archive', 'settings', 'fdp', 'spectro'];
  for (const tab of tabs) {
    const btn = document.getElementById('tab-btn-' + tab);
    if (!btn) continue;
    // Spectro is a one-station feature today: hide the tab entirely where
    // there is no grating camera, rather than showing an empty pane on all
    // 20+ stations. The registry flag decides, so no probe request is needed.
    const visible = canAccessTab(tab, host)
      && (tab !== 'spectro' || _hasSpectroCamera(host));
    btn.style.display = visible ? '' : 'none';
  }
  // If active tab is now hidden, switch to rms
  const activeBtn = document.querySelector('.sub-tab.active');
  if (activeBtn && activeBtn.style.display === 'none') {
    const rmsBtn = document.getElementById('tab-btn-rms');
    if (rmsBtn) switchTab('rms', rmsBtn);
  }
}

/* ─────────────────────────────────────────
   Role-dependent UI (the header account and admin links are
   dashboard-common.js initNavAuth)
───────────────────────────────────────── */
function applyRoleUi() {
  // Station switcher: hidden for guests (visibility:hidden preserves layout)
  const stationWrapper = document.getElementById('station-dropdown-wrapper');
  if (stationWrapper) stationWrapper.style.visibility = (state.USER_ROLE === 'guest') ? 'hidden' : '';
  // Tab visibility for current station
  if (state.activeStation) updateTabVisibility(state.activeStation);
}

/* ─────────────────────────────────────────
   Keyboard shortcuts
───────────────────────────────────────── */
// Arrow keys, Home and End move between the visible tabs of a role="tablist"
// (station sub-tabs, Station Admin inner tabs) and open the focused tab.
document.addEventListener('keydown', e => {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
  const tab = e.target.closest?.('[role="tab"]');
  const list = tab?.closest('[role="tablist"]');
  if (!list) return;
  const tabs = [...list.querySelectorAll('[role="tab"]')]
    .filter(t => !t.disabled && t.style.display !== 'none');
  const i = tabs.indexOf(tab);
  const next = e.key === 'Home' ? tabs[0]
    : e.key === 'End' ? tabs[tabs.length - 1]
    : tabs[(i + (e.key === 'ArrowLeft' ? -1 : 1) + tabs.length) % tabs.length];
  if (!next || next === tab) return;
  e.preventDefault();
  e.stopImmediatePropagation();
  next.focus();
  next.click();
});

// Enter and Space open a clickable card or image (role="button" on a div or
// img: clip cards, plot cards, sky domes) the way a click does. Only when the
// card itself has focus, so its inner buttons keep their own behaviour.
document.addEventListener('keydown', e => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const t = e.target;
  if (!t || t.getAttribute?.('role') !== 'button' || t.tagName === 'BUTTON') return;
  e.preventDefault();
  t.click();
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    closeCamModal(null);
    vdbCloseModal(null);
    stackCloseModal(null);
    appPanelClose(null);
    arcModalClose(null);
    window.spectroLightboxClose?.();
    if (typeof window.vdbCloseSyncedModal === 'function') window.vdbCloseSyncedModal();
  }
  if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
    const delta = e.key === 'ArrowLeft' ? -1 : 1;
    if (window.vdbModalIsOpen?.()) {
      e.preventDefault();
      vdbNavModal(delta);
    } else if (document.getElementById('stack-modal').classList.contains('open')) {
      e.preventDefault();
      stackNavModal(delta);
    }
  }
  // H in the clip player: hide this detection as a false positive and play
  // the next one (dashboard-vdb.js vdbHideAndNext).
  if ((e.key === 'h' || e.key === 'H') && !e.ctrlKey && !e.metaKey && !e.altKey && window.vdbModalIsOpen?.()) {
    const t = e.target;
    if (!(t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable))
        && window.vdbHideAndNext?.()) {
      e.preventDefault();
    }
  }
  // [ / ] jump to previous / next station — only when no modal is open
  // and the focus isn't inside an input/textarea/select.
  if (e.key === '[' || e.key === ']') {
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
    const anyModalOpen = document.querySelector('.cam-modal.open, .app-panel.open, .vm-backdrop.open, .mdm-backdrop.open');
    if (anyModalOpen) return;
    e.preventDefault();
    if (window.selectStationByOffset) window.selectStationByOffset(e.key === '[' ? -1 : 1);
  }
});


// The capture-window maths lives in dashboard-common.js (the header shows it on
// every page); re-exported here for the station toolbar and its tests.
export { fmtCaptureUTC, computeCaptureWindow };

function updateNextCapture(lat, lon) {
  const el = document.getElementById('next-capture');
  if (!el) return;
  const win = computeCaptureWindow(lat, lon);
  if (!win) { el.style.display = 'none'; return; }
  el.innerHTML = win.label;
  el.style.display = '';
}

/* ─────────────────────────────────────────
   Init
───────────────────────────────────────── */
(async function init() {
  // Clamp any date pickers to today (UTC) — captured nights can't be in the future.
  clampDateInputsToToday();
  // Fetch station config in parallel with the page's one session request,
  // which writes IS_ADMIN, AUTH_USER, USER_ROLE and USER_STATIONS into state.
  try {
    const [stationsResp, authData] = await Promise.all([
      fetch('/api/stations'),
      fetchAuthStatus(),
    ]);
    state.STATIONS_META = await stationsResp.json();
    if (!authData) throw new Error('session unknown');
  } catch (e) {
    document.getElementById('pane-rms').innerHTML =
      '<div class="offline">Failed to load station config</div>';
    return;
  }

  applyRoleUi();

  // Derive state.VDB_CAMERAS from config
  for (const [host, meta] of Object.entries(state.STATIONS_META)) {
    state.VDB_CAMERAS[host] = meta.cameras.map(c => c.code);
  }

  // Build station dropdown dynamically
  const wrapper = document.getElementById('station-dropdown-wrapper');
  const hosts = Object.keys(state.STATIONS_META);

  // Resolve active station from URL path (/station/<host>/<tab>) or DEFAULT_STATION
  const _pathParts = window.location.pathname.split('/').filter(Boolean);
  const _urlStation = _pathParts[0] === 'station' ? (_pathParts[1] || '') : '';
  const _urlTab     = _pathParts[0] === 'station' ? (_pathParts[2] || '') : '';

  // If the URL points to a station the user can't access, show a clear message
  if (_urlStation && !hosts.includes(_urlStation)) {
    const _safeStation = document.createElement('span');
    _safeStation.textContent = _urlStation;
    const _escapedStation = _safeStation.innerHTML;
    const msg = state.USER_ROLE === 'host'
      ? `Your account does not have access to station <b>${_escapedStation}</b>. Contact an admin to request access.`
      : `Station <b>${_escapedStation}</b> was not found.`;
    wrapper.textContent = _urlStation;
    document.querySelectorAll('.tab-pane').forEach(p => {
      p.classList.add('active');
      p.innerHTML = `<div class="offline" style="margin-top:2em">${msg}</div>`;
    });
    document.querySelectorAll('.sub-tabs').forEach(b => b.style.display = 'none');
    return;
  }
  if (!hosts.length) {
    wrapper.innerHTML = '';
    document.querySelectorAll('.tab-pane').forEach(p => {
      p.classList.add('active');
      p.innerHTML = '<div class="offline" style="margin-top:2em">No stations available for your account.</div>';
    });
    document.querySelectorAll('.sub-tabs').forEach(b => b.style.display = 'none');
    return;
  }

  state.activeStation = (hosts.includes(_urlStation) ? _urlStation : null)
    || (window.DEFAULT_STATION && hosts.includes(window.DEFAULT_STATION) ? window.DEFAULT_STATION : null)
    || hosts[0];

  function _stationDot(host) {
    const d = state.statusData[host];
    const cls = !d ? '' : (d.online ? 'dot-up' : 'dot-down');
    const lbl = !d ? 'status unknown' : (d.online ? 'station online' : 'station offline');
    return `<span class="pill-dot ${cls}" id="dot-${host}" role="img" aria-label="${host} ${lbl}"></span>`;
  }

  window.renderStationDropdown = function renderStationDropdown() {
    const meta = state.STATIONS_META[state.activeStation];
    const idx = hosts.indexOf(state.activeStation);
    const prevDisabled = idx <= 0 ? 'disabled' : '';
    const nextDisabled = idx >= hosts.length - 1 ? 'disabled' : '';
    wrapper.innerHTML = `
      <button type="button" class="station-nav-btn" id="station-prev-btn" ${prevDisabled}
              title="Previous station ([)" aria-label="Previous station"
              onclick="selectStationByOffset(-1)">&#8592;</button>
      <div class="station-dropdown" id="station-dropdown">
        <button type="button" class="station-dropdown-selected" id="station-dropdown-btn"
                aria-haspopup="listbox" aria-expanded="false" aria-controls="station-dropdown-menu"
                onclick="toggleStationDropdown(event)">
          ${_stationDot(state.activeStation)}
          <span id="station-dd-current" class="station-dd-code" translate="no">${escHtml(state.activeStation)}</span>
          <span class="station-dd-label" id="station-dd-current-label">${meta ? escHtml(meta.label) : ''}</span>
          <span class="station-dropdown-arrow" aria-hidden="true">&#9660;</span>
        </button>
        <div class="station-dropdown-menu" id="station-dropdown-menu" role="listbox"
             aria-label="Stations" onclick="event.stopPropagation()" onkeydown="stationDropdownKey(event)">
          ${hosts.map(h => {
            const m = state.STATIONS_META[h];
            const on = h === state.activeStation;
            return `<button type="button" role="option" aria-selected="${on}" tabindex="-1"
                            class="station-option${on ? ' active' : ''}" onclick="selectStationFromDropdown('${escHtml(h)}')">
              ${_stationDot(h)}
              <span class="station-dd-code" translate="no">${escHtml(h)}</span> <span class="station-dd-label">${m ? escHtml(m.label) : ''}</span>
            </button>`;
          }).join('')}
        </div>
      </div>
      <button type="button" class="station-nav-btn" id="station-next-btn" ${nextDisabled}
              title="Next station (])" aria-label="Next station"
              onclick="selectStationByOffset(1)">&#8594;</button>`;
  }

  window.selectStationByOffset = function(delta) {
    const idx = hosts.indexOf(state.activeStation);
    const target = hosts[idx + delta];
    if (target) selectStation(target);
  };

  renderStationDropdown();
  const _initMeta = state.STATIONS_META[state.activeStation];
  if (_initMeta) updateNextCapture(_initMeta.lat, _initMeta.lon);
  updateTabVisibility(state.activeStation);

  // Open/close the station menu and keep aria-expanded in step. Opening it
  // from the keyboard moves focus to the current station.
  function _setStationDropdown(open, { focus = false } = {}) {
    const dd = document.getElementById('station-dropdown');
    if (!dd) return;
    dd.classList.toggle('open', open);
    document.getElementById('station-dropdown-btn')?.setAttribute('aria-expanded', String(open));
    if (open && focus) {
      (dd.querySelector('.station-option.active') || dd.querySelector('.station-option'))?.focus();
    }
  }
  window.toggleStationDropdown = function(e) {
    e.stopPropagation();
    const dd = document.getElementById('station-dropdown');
    // e.detail is 0 for a keyboard-triggered click (Enter / Space).
    _setStationDropdown(!dd.classList.contains('open'), { focus: e.detail === 0 });
  };
  document.addEventListener('click', () => _setStationDropdown(false));

  // Arrow keys, Home and End move through the open station menu; Escape and
  // Tab close it.
  window.stationDropdownKey = function(e) {
    const opts = [...document.querySelectorAll('#station-dropdown-menu .station-option')];
    const i = opts.indexOf(document.activeElement);
    let next = null;
    if (e.key === 'ArrowDown') next = opts[Math.min(opts.length - 1, i + 1)];
    else if (e.key === 'ArrowUp') next = opts[Math.max(0, i - 1)];
    else if (e.key === 'Home') next = opts[0];
    else if (e.key === 'End') next = opts[opts.length - 1];
    else if (e.key === 'Escape' || e.key === 'Tab') {
      _setStationDropdown(false);
      if (e.key === 'Escape') document.getElementById('station-dropdown-btn')?.focus();
      return;
    }
    if (next) { e.preventDefault(); next.focus(); }
  };

  window.selectStationFromDropdown = function(host) {
    _setStationDropdown(false);
    selectStation(host);
  };

  window.updateStationDots = function() {
    for (const host of hosts) {
      const dot = document.getElementById('dot-' + host);
      if (!dot) continue;
      const d = state.statusData[host];
      dot.className = 'pill-dot' + (!d ? '' : (d.online ? ' dot-up' : ' dot-down'));
      const lbl = !d ? 'status unknown' : (d.online ? 'station online' : 'station offline');
      dot.setAttribute('aria-label', host + ' ' + lbl);
    }
  };

  // Restore tab from URL path, or default to rms
  const _startTab = [_tabBeforeInit, _urlTab, 'rms'].find(t => t && document.getElementById('tab-btn-' + t));
  const _startBtn = document.getElementById('tab-btn-' + _startTab);
  // ?cam=&date=&from=&to= open that camera, night and time window on
  // whichever tab the link names; the RMS tab scrolls to the night.
  Object.assign(state.view, _viewFromUrl(state.activeStation));
  if (_startTab === 'rms' && state.view.date) window._rmsPendingScrollDate = state.view.date;
  // ?clip=<filename> (an "Open in station / Video DB" link from a player):
  // the RMS or Video DB tab opens that clip's player once it is on screen.
  const _urlClip = new URLSearchParams(window.location.search).get('clip');
  window._pendingClip = /^[A-Za-z0-9._-]+\.(mkv|mp4)$/.test(_urlClip || '') ? _urlClip : null;
  // Canonical URL for the start view (no extra history entry)
  history.replaceState({ station: state.activeStation, tab: _startTab }, '', _stationUrl(state.activeStation, _startTab));
  window.vdbLoadAzimuths?.(state.activeStation);
  switchTab(_startTab, _startBtn, { push: false });

  // Back/forward navigation: restore station, tab and view from the entry,
  // then re-render. The pane must be rebuilt even when only the station
  // changed, or Back from station B to A shows A's name over B's content.
  window.addEventListener('popstate', e => {
    const s = e.state;
    // A modal's own entry (Forward onto it) has nothing to restore, and
    // neither does Back that only dismissed a player (dashboard-common
    // closed it): the pane still shows this entry's station, tab and view.
    // Rebuilding it anyway reloaded the Video DB and lost the scroll.
    if (!s || s._rovModal) return;
    if (s.station === state.activeStation && (s.tab || state.activeTab) === state.activeTab
        && _urlShowsView(state.activeStation)) return;
    if (s.station && hosts.includes(s.station) && s.station !== state.activeStation) {
      state.activeStation = s.station;
      renderStationDropdown();
      const _pm = state.STATIONS_META[s.station];
      if (_pm) updateNextCapture(_pm.lat, _pm.lon);
      updateTabVisibility(s.station);
      window._vdbAzimuths = {};
      window.vdbLoadAzimuths?.(s.station);
      if (nightTimer)          { clearInterval(nightTimer);          nightTimer          = null; }
      if (sysadminVitalsTimer) { clearInterval(sysadminVitalsTimer); sysadminVitalsTimer = null; }
      if (sysadminStorageTimer){ clearInterval(sysadminStorageTimer);sysadminStorageTimer= null; }
    }
    Object.assign(state.view, _viewFromUrl(state.activeStation));
    const tab = s.tab || state.activeTab;
    const tabBtn = document.getElementById('tab-btn-' + tab);
    if (!tabBtn) return;
    window.vdbLoadedStation = null;   // re-init the Video DB with the restored view
    if (tab === 'rms' && state.view.date) window._rmsPendingScrollDate = state.view.date;
    switchTab(tab, tabBtn, { push: false });
  });

  // Initial fetch for all stations (one round-trip each instead of N).
  fetchAllStatus();
  fetchAllVitals();
  startTimers();

  // Retry until we have data for the active station
  (function poll() {
    if (!state.statusData[state.activeStation]) {
      setTimeout(() => { fetchStatus(state.activeStation); poll(); }, 4000);
    }
  })();
})();

// Expose to global scope for inline onclick handlers
window.switchTab = switchTab;
window.refreshCurrent = refreshCurrent;
window.closeCamModal = closeCamModal;
window.stackCloseModal = stackCloseModal;
window.stackNavModal = stackNavModal;
window.toggleCamLive = toggleCamLive;
window.expandCam = expandCam;
window.fdpOpenRmsPlot = fdpOpenRmsPlot;
window.fdpOnNightChange = fdpOnNightChange;
window.fdpStepNight = fdpStepNight;
window._fdpTileLoaded = _fdpTileLoaded;
window._fdpLen = _fdpLen;
window._fdpTileMissing = _fdpTileMissing;
window.vdbSkyDomeOpenModal = vdbSkyDomeOpenModal;
window.stackOpenModal = stackOpenModal;
window.canAccessTab = canAccessTab;
window.fetchOnce = fetchOnce;
window.renderTimelapses = renderTimelapses;
window.askConfirm = askConfirm;
window.fetchStatus = fetchStatus;
window._accessBanner = _accessBanner;
window._isAbort = _isAbort;
window.liveStreamError = liveStreamError;
window.liveStreamLoaded = liveStreamLoaded;
window.vdbSkyDomeOnError = vdbSkyDomeOnError;
// Station helpers the System Admin tab (dashboard-sysadmin.js) calls
window.mkCameraWindows = mkCameraWindows;
window.mkServices = mkServices;
window.mkStorage = mkStorage;
window._ownsStation = _ownsStation;
window.fetchSettings = fetchSettings;
