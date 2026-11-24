// Detection grid view (issue #313): rows = time buckets, columns = cameras.
// A row with many filled cells is a multi-station coincidence -- the whole
// point of the view is making those jump out without cross-referencing
// per-station pages.
//
// This module is loaded by events.html and drives the "Grid" tab. The heavy
// lifting (fan-out across stations) happens server-side in
// /api/detections/grid/<date>; this file only requests, filters and paints.
//
// Pure helpers (_gridAgeLabel, _gridFilterColumns) are exported
// so they can be unit-tested in isolation (tests/js/dashboard-grid.test.js).

import { escHtml, viewQuery, reportTabView, clipStatus, parseHHMM } from './dashboard-common.js';
import { openTimePicker, wireTimeField } from './dashboard-time-picker.js';
import { withHidden, showingHidden, hiddenNoteText, markAll, hideButtonHtml, hideTarget, toggleHide, updateToggle } from './dashboard-hidden.js';

// ── Pure helpers (exported for unit tests) ──────────────────────────────────

/** Default footage start = "now minus 5 minutes" in UTC, as minutes-of-day.
 *
 *  The all-footage view is a live tool: an operator opens it to see what the
 *  fleet is recording *right now*, so the window must track the wall clock
 *  rather than sit on a fixed evening hour. Computed at load time (never a
 *  constant) and wrapped across midnight so 00:02 UTC yields 23:57, not a
 *  negative minute. `now` is injectable for tests. */
export function _gridDefaultFromMin(now = new Date()) {
  const mins = now.getUTCHours() * 60 + now.getUTCMinutes();
  return ((mins - 5) % 1440 + 1440) % 1440;
}

/** Pull a minutes-of-day time into a capture window given as {lo, hi} in the
 *  sliders' noon-anchored minutes (twilight-slider nightDomain). With
 *  `room`, the time is also kept at least that many minutes before the
 *  window's end, so a start leaves space for the footage span after it.
 *  Pure: unit-tested. */
export function _gridClampToWindow(min, win, room = 0) {
  if (min == null || !win) return min;
  const n = ((min - 720) % 1440 + 1440) % 1440;
  const c = Math.max(win.lo, Math.min(win.hi - room, n));
  return (c + 720) % 1440;
}

/** Resolve a clicked `.grid-thumb` dataset into the args `openSingleVideo`
 *  needs, or null when the thumb is not playable.
 *
 *  A thumb is playable only when it carries both a `host` (station key) and a
 *  `fn` (archived clip filename). Detection/footage cells for a *pending* clip
 *  render a thumbnail from the stack that arrives ahead of the bytes but leave
 *  `data-fn` empty; those correctly no-op. Guarding on `host` too means a cell
 *  that somehow lost its station key never opens a modal against a broken
 *  `/api/cached-video//<cam>/...` URL. Pure so the resolution is unit-testable
 *  without a DOM or a real click. */
export function _gridResolveThumb(dataset) {
  if (!dataset) return null;
  const host = dataset.host || '';
  const fn = dataset.fn || '';
  if (!host || !fn) return null;
  return {
    host,
    cam: dataset.cam || '',
    date: dataset.date || '',
    fn,
    offset: Number(dataset.offset) || 0,
    mtime: dataset.mtime || '',
    stack: dataset.stack || '',
    label: dataset.label || '',
    // Already re-encoded and delivered: the modal shows "Processed" instead of
    // offering a second Process run.
    reencoded: dataset.reencoded === '1',
    // Real per-clip lock state (detections-mode thumbs are always locked;
    // footage-mode thumbs carry the station's actual flag) — without this the
    // modal's Lock button can't tell a locked clip from an ordinary chunk.
    locked: dataset.locked === '1',
    lockType: dataset.lockType || null,
    // Hidden as a false positive (dashboard-hidden.js): the modal offers Unhide.
    hidden: dataset.hidden === '1',
  };
}

/** Stable identity for a clip across renders: host|cam|date|filename. Two
 *  thumbs of the same archived clip (e.g. the same detection shown in two grid
 *  modes) collapse to one selection. Pure so the key is unit-testable. */
export function _gridClipKey(clip) {
  if (!clip) return '';
  return [clip.host || '', clip.cam || '', clip.date || '', clip.fn || ''].join('|');
}

/** Turn a resolved thumb (see `_gridResolveThumb`) into the video shape
 *  `MultiDetModal.open({ videos })` expects, mirroring how `openEventModal`
 *  builds witness videos in dashboard-events.js. Returns null for a thumb that
 *  cannot be played (no host / no filename). `stack` and `label` ride on the
 *  thumb dataset (added in `_gridCellHtml` / `_footageStripHtml`); when absent
 *  the clip still plays, just without a stack image or a station label. */
export function _gridClipToVideo(clip) {
  if (!clip || !clip.host || !clip.fn) return null;
  const enc = encodeURIComponent(clip.fn);
  const stackUrl = clip.stack
    ? `/stack/${clip.host}/${clip.cam}/${clip.date}/${clip.stack}`
    : null;
  return {
    cam:         clip.cam,
    station:     clip.host,
    date:        clip.date,
    url:         `/api/cached-video/${clip.host}/${clip.cam}/${clip.date}/${enc}?format=mp4`,
    offset:      Number(clip.offset) || 0,
    label:       clip.label || clip.cam,
    downloadUrl: `/shortclip/${clip.host}/${clip.cam}/${clip.date}/${enc}`,
    stackUrl,
    filename:    clip.fn,
    // A detection clip's tile carries Hide (MultiDetModal, dashboard-hidden.js).
    ...(clip.lockType === 'detection' && clip.mtime
      ? { hide: { target: hideTarget({ meteor_time: clip.mtime }, clip.cam), hidden: !!clip.hidden } }
      : {}),
  };
}

/** Map an ordered list of resolved clips to the `videos` array MultiDetModal
 *  wants, dropping any that can't build a video shape (e.g. lost their host).
 *  Pure so the selection → modal payload can be unit-tested without a DOM. */
export function _gridSelectionVideos(clips) {
  const out = [];
  for (const clip of clips || []) {
    const v = _gridClipToVideo(clip);
    if (v) out.push(v);
  }
  return out;
}

/** Human "age" of an ISO timestamp relative to `now` (default: real now).
 *  Returns e.g. "2h ago", "1d ago", "just now". `now` is injectable for tests. */
export function _gridAgeLabel(iso, now = Date.now()) {
  if (!iso) return '';
  const t = new Date(iso + (iso.endsWith('Z') ? '' : 'Z')).getTime();
  if (!Number.isFinite(t)) return '';
  let secs = Math.floor((now - t) / 1000);
  if (secs < 0) secs = 0;
  if (secs < 60) return 'just now';
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

/** Keep only columns that have at least one cell across the supplied rows.
 *  Empty cameras (no detections all night) would otherwise produce a wall of
 *  blank columns. Preserves the server-provided column order. */
export function _gridFilterColumns(columns, rows) {
  const seen = new Set();
  for (const row of rows || []) {
    for (const cam of Object.keys(row.cells || {})) seen.add(cam);
  }
  return (columns || []).filter(c => seen.has(c.cam));
}

// ── State ───────────────────────────────────────────────────────────────────

let _gridData = null;          // last fetched { date, bucket_min, columns, rows }
let _gridInited = false;
let _gridMode = 'footage';     // 'footage' (all continuous chunks) | 'detections' (locked only)
let _gridShowOther = false;    // hide non-RO/MD/OR (e.g. Berlin) cameras by default
const FOOTAGE_MAX_MIN = 5;     // footage shows every clip, so cap the window (5 min ≈ 15 clips/cam)

// Multi-select (issue #896): while select mode is on, clicking a thumb toggles
// it into the selection instead of opening it; "View selected" opens them all
// together in the shared MultiDetModal. The selection Map keeps insertion order
// so the modal grid mirrors pick order; keyed by host|cam|date|filename so the
// same clip never lands twice. Selection persists across nights/stations/modes
// (deliberately cross-date) until the operator clears or views it.
let _gridSelectMode = false;
const _gridSelected = new Map();   // clipKey -> resolved clip (see _gridResolveThumb)
// MultiDetModal synchronizes playback across every selected clip; past a dozen
// that is impractical (and the dome path only samples the first 8), so cap the
// set and refuse further adds with a hint rather than degrade silently.
export const GRID_SELECT_MAX = 12;

/** A camera outside Romania/Moldova (e.g. Berlin DE*). Hidden by default:
 *  a meteor never appears from both a foreign site and Romania/Moldova, so
 *  those rows are pure dead space in the coincidence grid — the "Other
 *  stations" toggle re-adds them. Exported for unit tests. */
export function _gridIsOther(cam) {
  return !!cam && !/^(RO|MD|OR)/i.test(cam);
}

/** Columns/cameras to actually paint, in server (station-number) order: drop
 *  foreign ("other") ones unless the toggle is on, and drop empty ones unless
 *  `keepEmpty`. The footage grid keeps them, so a station that stopped
 *  capturing shows as a dimmed row instead of vanishing and making the
 *  station order look broken; the detections grid drops them, because most
 *  cameras have no detection in a given window. Exported for unit tests. */
export function _gridVisibleColumns(columns, rows, { keepEmpty = false, showOther = _gridShowOther, onlyCams = _gridCamFilter } = {}) {
  const cols = keepEmpty ? (columns || []) : _gridFilterColumns(columns, rows);
  // A camera list from a map point (?cams=) is exact: it shows those cameras,
  // foreign ones included, and nothing else.
  if (onlyCams && onlyCams.size) return cols.filter(c => onlyCams.has(c.cam));
  return showOther ? cols : cols.filter(c => !_gridIsOther(c.cam));
}

/** Camera codes from a `cams=RO000E,RO000G` URL value: upper-cased, valid
 *  codes only, de-duplicated, in order. Null when none survive. */
export function _gridParseCams(value) {
  const out = [];
  for (const raw of String(value || '').split(',')) {
    const c = raw.trim().toUpperCase();
    if (/^[A-Z0-9]{4,8}$/.test(c) && !out.includes(c)) out.push(c);
  }
  return out.length ? out.slice(0, 100) : null;
}

// Cameras picked on the overview map (?cams=), or null for every camera.
let _gridCamFilter = (() => {
  const cams = _gridParseCams(new URLSearchParams(globalThis.location?.search || '').get('cams'));
  return cams ? new Set(cams) : null;
})();

/** The `cams` URL value for the current filter, or null (dashboard-events.js
 *  keeps it in the address bar). */
export function gridCamsParam() {
  return _gridCamFilter ? [..._gridCamFilter].join(',') : null;
}

function _gridUpdateCamChip() {
  const chip = document.getElementById('grid-cam-filter');
  if (!chip) return;
  const n = _gridCamFilter ? _gridCamFilter.size : 0;
  chip.hidden = !n;
  const text = chip.querySelector('.grid-cam-filter-text');
  if (text) {
    text.textContent = `Showing ${n} camera${n === 1 ? '' : 's'} from a map point`;
    text.title = n ? [..._gridCamFilter].join(', ') : '';
  }
}

function _gridClearCamFilter() {
  _gridCamFilter = null;
  _gridUpdateCamChip();
  window.evSyncUrl?.();
  if (_gridData) renderGrid(_gridData);
}

/** "gmnro10 · Roit": the station ID with its place, so the station-number
 *  order of the rows is readable. Falls back to whichever part is present. */
export function _gridStationLabel(col) {
  const id = (col && col.host_key) || '';
  const place = (col && col.station_label) || '';
  if (id && place && id !== place) return `${id} · ${place}`;
  return id || place;
}

// ── Rendering ───────────────────────────────────────────────────────────────

function _gridStackUrl(date, cell) {
  return cell.stack
    ? `/stack/${cell.host_key}/${cell.cam}/${date}/${cell.stack}`
    : null;
}

export function _gridCellHtml(date, cam, cells, showHidden = showingHidden()) {
  // Detections hidden as false positives: left out, or dimmed with Show hidden.
  cells = (cells || []).filter(c => showHidden || !c.hidden);
  if (!cells.length) {
    return `<td class="grid-cell grid-cell-empty"></td>`;
  }
  const multi = cells.some(c => c.multi);
  const inner = cells.map(c => {
    const stackUrl = _gridStackUrl(date, c);
    const offset = c.detection_offset_s || 0;
    const timeLabel = escHtml((c.meteor_time || '').slice(11, 19));
    // A pending detection has no archived clip/stack yet (bytes in transit);
    // data-fn stays empty so the delegated click handler ignores it.
    const thumb = stackUrl
      ? `<img src="${escHtml(stackUrl)}" loading="lazy" alt="${escHtml(cam)} ${timeLabel}">`
      : `<div class="grid-thumb-noimg">${c.pending ? escHtml(clipStatus(c).label) : 'no img'}</div>`;
    const pendingNote = !c.pending ? ''
      : c.pruned ? ' — video removed after 14 days'
      : c.no_clip ? ' — no video from this camera' : ` — ${clipStatus(c).label}`;
    const hdNote = hiddenNoteText(c);
    const title = `${cam} ${timeLabel} UTC${pendingNote}${hdNote ? ` — ${hdNote}` : ''}`;
    // The click is wired via a single delegated listener on #grid-main (see
    // gridInit). Values ride on HTML-escaped data-* attributes instead of an
    // inline onclick, so nothing is interpolated into executable code.
    const focusable = !c.pending && c.filename ? ' role="button" tabindex="0"' : '';
    return `<div class="grid-thumb${c.multi ? ' multi' : ''}${c.pending ? ' pending' : ''}${c.hidden ? ' hd-hidden' : ''}" title="${escHtml(title)}"${focusable}`
      + ` data-host="${escHtml(c.host_key)}" data-cam="${escHtml(c.cam)}" data-date="${escHtml(date)}"`
      + ` data-fn="${escHtml(c.filename || '')}" data-offset="${escHtml(offset)}" data-mtime="${escHtml(c.meteor_time || '')}"`
      + ` data-stack="${escHtml(c.stack || '')}" data-label="${escHtml(c.station_label || '')}"`
      // Detections mode only ever lists locked meteors (see module docstring).
      + ` data-locked="1" data-lock-type="${escHtml(c.lock_type || 'detection')}"`
      + ` data-reencoded="${c.reencoded ? '1' : ''}" data-hidden="${c.hidden ? '1' : ''}">
      ${thumb}${c.meteor_time ? hideButtonHtml(c) : ''}
    </div>`;
  }).join('');
  return `<td class="grid-cell${multi ? ' grid-cell-multi' : ''}">
    <div class="grid-cell-inner">${inner}</div>
  </td>`;
}

/** The Grid's time window as {from, to} for links ({} when unset). */
function _gridWindow() {
  const from = document.getElementById('grid-from')?.value || '';
  const to = document.getElementById('grid-to')?.value || '';
  const ok = v => /^([01]\d|2[0-3]):[0-5]\d$/.test(v);
  return ok(from) && ok(to) ? { from, to } : {};
}

/** One click from a camera name to its Video DB on the Grid's night and
 *  window; the station name opens the station on that night. */
export function gridCamUrl(host, cam, date, win = {}) {
  return `/station/${encodeURIComponent(host)}/videodb` + viewQuery({ cam, date: date || null, ...win });
}
export function gridStationUrl(host, date) {
  return `/station/${encodeURIComponent(host)}/rms` + viewQuery({ date: date || null });
}

function _gridCamLink(col, label) {
  if (!col.host_key) return escHtml(label);
  const date = document.getElementById('ev-date')?.value || null;
  return `<a class="ev-link" href="${escHtml(gridCamUrl(col.host_key, col.cam, date, _gridWindow()))}"
    title="Open ${escHtml(col.cam)} in the Video DB">${escHtml(label)}</a>`;
}

/** Column header: cameras grouped under their station label, shared by both
 *  grid modes. Returns the full `<thead>` markup. */
function _gridHeadHtml(columns) {
  let head = '<thead><tr class="grid-station-row"><th class="grid-corner"></th>';
  let lastStation = null;
  let span = 0;
  const stationCells = [];
  let lastHost = null;
  for (const col of columns) {
    if (col.station_label !== lastStation) {
      if (lastStation !== null) stationCells.push({ label: lastStation, host: lastHost, span });
      lastStation = col.station_label;
      lastHost = col.host_key || null;
      span = 1;
    } else {
      span += 1;
    }
  }
  if (lastStation !== null) stationCells.push({ label: lastStation, host: lastHost, span });
  const date = document.getElementById('ev-date')?.value || null;
  for (const s of stationCells) {
    const text = _gridStationLabel({ host_key: s.host, station_label: s.label });
    const label = s.host
      ? `<a class="ev-link" href="${escHtml(gridStationUrl(s.host, date))}" title="Open ${escHtml(s.label || s.host)}">${escHtml(text)}</a>`
      : escHtml(text);
    head += `<th class="grid-station-hdr" colspan="${s.span}">${label}</th>`;
  }
  head += '</tr><tr class="grid-cam-row"><th class="grid-corner">time (UTC)</th>';
  for (const col of columns) {
    head += `<th class="grid-cam-hdr" title="${escHtml(col.station_label || '')} — ${escHtml(col.cam)}">${_gridCamLink(col, col.cam)}</th>`;
  }
  head += '</tr></thead>';
  return head;
}

/** Dispatch to the renderer for the active grid mode, then re-apply the
 *  selection marks — every render rebuilds #grid-main's innerHTML, so the
 *  ".grid-thumb-selected" classes have to be repainted from the persisted set. */
function renderGrid(data) {
  if (_gridMode === 'footage') renderFootageGrid(data);
  else renderDetectionGrid(data);
  _gridSelectApplyMarks();
}

function renderDetectionGrid(data) {
  const el = document.getElementById('grid-main');
  if (!el) return;
  if (!data) {
    el.innerHTML = '<div class="ev-empty">No data loaded.</div>';
    return;
  }
  const date = data.date || '';
  const rows = data.rows || [];
  const columns = _gridVisibleColumns(data.columns || [], rows);

  if (!rows.length || !columns.length) {
    el.innerHTML = `<div class="ev-empty">No detections in this window.<br>
      <span style="font-size:12px">Widen the time range or pick a busier night.</span></div>`;
    return;
  }

  const head = _gridHeadHtml(columns);

  // Body: one row per populated time bucket, newest first.
  let body = '<tbody>';
  for (const row of rows) {
    const newest = _gridRowNewest(row);
    const age = newest ? _gridAgeLabel(newest) : '';
    const rowCls = row.has_multi ? ' grid-row-multi' : '';
    body += `<tr class="grid-row${rowCls}">`;
    body += `<th class="grid-time-hdr" title="${row.total} detection${row.total === 1 ? '' : 's'} across ${row.station_count} station${row.station_count === 1 ? '' : 's'}">
      <span class="grid-time">${escHtml(row.label)}</span>
      <span class="grid-age">${escHtml(age)}</span>
    </th>`;
    for (const col of columns) {
      body += _gridCellHtml(date, col.cam, (row.cells || {})[col.cam]);
    }
    body += '</tr>';
  }
  body += '</tbody>';

  el.innerHTML = `<div class="grid-scroll"><table class="grid-table">${head}${body}</table></div>`;
}

/** A grid thumbnail the archive does not hold yet (the station's FF thumbnail
 *  or colour stack is still on its way) answers 404: swap the browser's
 *  broken-image icon for a tile that says so. The thumb keeps its dataset, so
 *  a click still opens the clip. */
export function _gridThumbImgFailed(img) {
  if (!img || img.tagName !== 'IMG' || !img.closest('.grid-thumb')) return;
  const tile = document.createElement('div');
  tile.className = 'grid-thumb-noimg';
  tile.textContent = 'not uploaded yet';
  img.replaceWith(tile);
}

// ── Footage mode (all continuous chunks) ────────────────────────────────────

/** One cell = EVERY clip that camera recorded in this (camera, time-bucket)
 *  slot, rendered as a row of thumbnails. The window is capped upstream so a
 *  cell only ever holds a handful of 20 s clips. */
export function _footageCellHtml(date, cam, cell, label = '') {
  if (!cell || !cell.chunks || !cell.chunks.length) {
    return `<td class="grid-cell grid-cell-empty"></td>`;
  }
  return `<td class="grid-cell grid-cell-footage${cell.has_detection ? ' grid-cell-detection' : ''}">`
    + `<div class="grid-cell-inner">${_footageStripHtml(date, cam, cell.chunks, label)}</div></td>`;
}

/** Build the clip thumbnails for a cell: one per 20 s chunk, time-ordered.
 *  `label` is the camera's station label (from the column), threaded onto each
 *  thumb so a multi-select can build the MultiDetModal video shape without a
 *  second lookup. */
export function _footageStripHtml(date, cam, chunks, label = '') {
  return (chunks || []).map(c => {
    const host = c.host_key || '';
    const t = escHtml((c.time || '').slice(0, 8));
    const stackUrl = c.stack ? `/stack/${host}/${cam}/${date}/${c.stack}` : null;
    const thumb = stackUrl
      ? `<img src="${escHtml(stackUrl)}" loading="lazy" alt="${escHtml(cam)} ${t}">`
      : `<div class="grid-thumb-noimg">no img</div>`;
    return `<div class="grid-thumb${c.locked ? ' detection' : ''}" title="${escHtml(cam)} ${t} UTC" role="button" tabindex="0" `
      + `data-host="${escHtml(host)}" data-cam="${escHtml(cam)}" data-date="${escHtml(date)}" `
      + `data-fn="${escHtml(c.filename || '')}" data-offset="${escHtml(c.detection_offset_s || 0)}" data-mtime="${escHtml(c.meteor_time || '')}" `
      + `data-stack="${escHtml(c.stack || '')}" data-label="${escHtml(label || '')}" `
      + `data-locked="${c.locked ? '1' : '0'}" data-lock-type="${escHtml(c.lock_type || '')}" `
      + `data-reencoded="${c.reencoded ? '1' : ''}">`
      + `${thumb}</div>`;
  }).join('');
}

/** Transposed footage matrix: cameras down the Y axis, time across the X axis.
 *  The server returns time-bucket rows with per-camera cells; we pivot so each
 *  table row is a camera and each column is a time bucket (earliest left). */
function renderFootageGrid(data) {
  const el = document.getElementById('grid-main');
  if (!el) return;
  if (!data) {
    el.innerHTML = '<div class="ev-empty">No data loaded.</div>';
    return;
  }
  const date = data.date || '';
  const timeBuckets = data.rows || [];                              // X axis (time, ascending)
  // Y axis: every camera, those without clips in the window included (dimmed).
  const cameras = _gridVisibleColumns(data.columns || [], timeBuckets, { keepEmpty: true });
  const withFootage = _gridVisibleColumns(data.columns || [], timeBuckets);

  const banner = data.degraded
    ? `<div class="grid-banner">Continuous footage for this night has rotated off the stations
       (kept ~2 days). Showing locked detections only.</div>`
    : '';

  if (!timeBuckets.length || !withFootage.length) {
    const scope = _gridCamFilter ? ' from the cameras picked on the map' : '';
    el.innerHTML = banner + `<div class="ev-empty">No footage${scope} in this window.<br>
      <span style="font-size:12px">Pick a recent night and a start time (≤ ${data.max_window_min || 5} min shown).</span></div>`;
    return;
  }

  // Header: corner + one column per time bucket (X axis), sticky at top.
  let head = '<thead><tr><th class="grid-corner">camera \\ time (UTC)</th>';
  for (const tb of timeBuckets) {
    const n = tb.total || 0;
    head += `<th class="grid-cam-hdr" title="${escHtml(tb.label)} UTC — ${n} clip${n === 1 ? '' : 's'} across ${tb.station_count} station${tb.station_count === 1 ? '' : 's'}">${escHtml(tb.label)}</th>`;
  }
  head += '</tr></thead>';

  // Body: one row per camera (Y axis); cells walk the time buckets (X axis).
  let body = '<tbody>';
  for (const camCol of cameras) {
    const cam = camCol.cam;
    const camHasDet = timeBuckets.some(tb => ((tb.cells || {})[cam] || {}).has_detection);
    const camEmpty = !timeBuckets.some(tb => (((tb.cells || {})[cam] || {}).chunks || []).length);
    const station = _gridStationLabel(camCol);
    body += `<tr class="grid-row${camHasDet ? ' grid-row-multi' : ''}${camEmpty ? ' grid-row-empty' : ''}">`;
    body += `<th class="grid-time-hdr" title="${escHtml(station)} — ${escHtml(cam)}${camEmpty ? ' — no footage in this window' : ''}">
      <span class="grid-time">${_gridCamLink(camCol, cam)}${camEmpty ? ' <span class="grid-nofootage">no footage</span>' : ''}</span>
      <span class="grid-age">${escHtml(station)}</span>
    </th>`;
    for (const tb of timeBuckets) {
      body += _footageCellHtml(date, cam, (tb.cells || {})[cam], camCol.station_label || '');
    }
    body += '</tr>';
  }
  body += '</tbody>';

  el.innerHTML = banner + `<div class="grid-scroll"><table class="grid-table">${head}${body}</table></div>`;
}

/** Update the toolbar hint to match the active grid mode. */
function _gridUpdateHint() {
  const hint = document.querySelector('#pane-grid .grid-hint');
  if (!hint) return;
  hint.textContent = _gridMode === 'footage'
    ? `Cameras down, time across: every clip in the window (up to ${FOOTAGE_MAX_MIN} min), including cameras that never triggered. Continuous video is kept ~2 days.`
    : 'Rows are time buckets, columns cameras. A row with many filled cells is a multi-station event.';
}

/** Newest meteor_time in a row (for the age label). */
function _gridRowNewest(row) {
  let newest = '';
  for (const cells of Object.values(row.cells || {})) {
    for (const c of cells) {
      if (c.meteor_time && c.meteor_time > newest) newest = c.meteor_time;
    }
  }
  return newest;
}

// ── Data loading ────────────────────────────────────────────────────────────

async function gridLoad() {
  const el = document.getElementById('grid-main');
  if (!el) return;
  const datePicker = document.getElementById('ev-date');
  const date = datePicker ? datePicker.value : null;
  if (!date) {
    el.innerHTML = '<div class="ev-empty">Pick a night.</div>';
    return;
  }
  const bucket = document.getElementById('grid-bucket')?.value || '1';
  const fromEl = document.getElementById('grid-from');
  const toEl = document.getElementById('grid-to');
  // No footage outside the fleet's capture window: times are pulled into it.
  _gridWin = await _gridCaptureWindow(date);
  let fromMin = parseHHMM(fromEl?.value);
  let toMin = parseHHMM(toEl?.value);
  if (_gridMode !== 'footage') {
    fromMin = _gridClampToWindow(fromMin, _gridWin);
    toMin = _gridClampToWindow(toMin, _gridWin);
    if (fromEl && fromMin != null) fromEl.value = _gridMinToHHMM(fromMin);
    if (toEl && toMin != null) toEl.value = _gridMinToHHMM(toMin);
  }

  // Footage shows every clip, so enforce a window of at most FOOTAGE_MAX_MIN.
  // Default the start to "now minus 5 minutes" UTC (recomputed on every load,
  // so it tracks the wall clock) when blank, derive/clamp the end, and reflect
  // the clamped values back into the inputs so the user sees what's shown.
  if (_gridMode === 'footage') {
    if (fromMin == null) fromMin = _gridDefaultFromMin();
    // In daytime "now minus 5" falls outside the window: the start goes to
    // the nearest end of the night, with the footage span still inside it.
    fromMin = _gridClampToWindow(fromMin, _gridWin, FOOTAGE_MAX_MIN);
    toMin = _gridClampToWindow(toMin, _gridWin);
    const span = toMin == null ? null : ((toMin - fromMin) + 1440) % 1440;
    if (span == null || span === 0 || span > FOOTAGE_MAX_MIN) {
      toMin = (fromMin + FOOTAGE_MAX_MIN) % 1440;
    }
    if (fromEl) fromEl.value = _gridMinToHHMM(fromMin);
    if (toEl) toEl.value = _gridMinToHHMM(toMin);
  }

  const params = new URLSearchParams({ bucket });
  if (fromMin != null) params.set('from', _gridMinToHHMM(fromMin));
  if (toMin != null) params.set('to', _gridMinToHHMM(toMin));

  _gridUpdateHint();
  window.evSyncUrl?.();   // the Grid window (as clamped above) goes into the URL
  const base = _gridMode === 'footage' ? '/api/footage/grid/' : '/api/detections/grid/';
  const gridUrl = `${base}${date}?${params.toString()}`;
  el.innerHTML = '<div class="ev-loading"><div class="ev-spinner"></div> Loading grid…</div>';
  try {
    const r = await fetch(_gridMode === 'footage' ? gridUrl : withHidden(gridUrl));
    if (!r.ok) throw new Error(r.statusText);
    _gridData = await r.json();
    renderGrid(_gridData);
    gridSyncHiddenToggle();
    window.evRestoreScroll?.();
  } catch (e) {
    el.innerHTML = `<div class="ev-empty">Failed to load grid: ${escHtml(e.message)}</div>`;
  }
}

// The fleet's capture window per night (/api/twilight/network), in the
// sliders' noon-anchored units; null when unknown (no limit).
let _gridWin = null;
const _gridWinCache = new Map();
function _gridCaptureWindow(date) {
  if (!_gridWinCache.has(date)) {
    _gridWinCache.set(date, fetch(`/api/twilight/network/${date}`)
      .then(r => (r.ok ? r.json() : null))
      .then(net => {
        const d = net && net.capture_start_min != null ? window.twilightSlider?.nightDomain(net) : null;
        return d && (d.lo > 0 || d.hi < 1440) ? d : null;
      })
      .catch(() => null));
  }
  return _gridWinCache.get(date);
}

function _gridMinToHHMM(min) {
  return `${String(Math.floor(min / 60)).padStart(2, '0')}:${String(min % 60).padStart(2, '0')}`;
}

// ── Multi-select (issue #896) ───────────────────────────────────────────────

/** Re-paint the "selected" ring on every currently-rendered thumb whose clip is
 *  in the selection set. Runs after each render (the grid re-creates its HTML)
 *  and idempotently, so it can also be called on individual toggles. */
function _gridSelectApplyMarks() {
  const el = document.getElementById('grid-main');
  if (!el) return;
  el.classList.toggle('grid-select-mode', _gridSelectMode);
  for (const thumb of el.querySelectorAll('.grid-thumb')) {
    const clip = _gridResolveThumb(thumb.dataset);
    const sel = !!clip && _gridSelected.has(_gridClipKey(clip));
    thumb.classList.toggle('grid-thumb-selected', sel);
  }
}

/** Sync the toolbar: toggle button pressed-state, "View selected (N)" label and
 *  enabled-state, "Clear" visibility. All guarded so a missing button no-ops. */
function _gridUpdateSelectUI() {
  const toggle = document.getElementById('grid-select-toggle');
  if (toggle) {
    toggle.classList.toggle('active', _gridSelectMode);
    toggle.setAttribute('aria-pressed', String(_gridSelectMode));
  }
  const n = _gridSelected.size;
  const view = document.getElementById('grid-view-selected');
  if (view) {
    view.textContent = `View selected (${n})`;
    view.disabled = n < 1;
    view.style.display = _gridSelectMode ? '' : 'none';
  }
  const clear = document.getElementById('grid-clear-selected');
  if (clear) {
    clear.disabled = n < 1;
    clear.style.display = _gridSelectMode ? '' : 'none';
  }
}

/** Add/remove a resolved clip from the selection, enforcing the size cap. */
function _gridToggleSelectClip(clip) {
  if (!clip) return;
  const key = _gridClipKey(clip);
  if (_gridSelected.has(key)) {
    _gridSelected.delete(key);
  } else {
    if (_gridSelected.size >= GRID_SELECT_MAX) {
      (window._toast || window.alert || (() => {})).call(
        null, `You can select at most ${GRID_SELECT_MAX} clips at once.`);
      return;
    }
    _gridSelected.set(key, clip);
  }
  _gridSelectApplyMarks();
  _gridUpdateSelectUI();
}

/** Drop the whole selection (keeps select mode on). */
function _gridClearSelection() {
  _gridSelected.clear();
  _gridSelectApplyMarks();
  _gridUpdateSelectUI();
}

/** Enter/leave select mode. Leaving keeps whatever is selected so a stray
 *  toggle doesn't discard a half-built set; the "Clear" button is the explicit
 *  reset. */
function _gridSetSelectMode(on) {
  _gridSelectMode = on;
  _gridSelectApplyMarks();
  _gridUpdateSelectUI();
}

/** Open every selected clip together in the shared MultiDetModal. Delegates to
 *  window.openMultiVideos (exposed by dashboard-events.js), which reuses the one
 *  page-level modal instance instead of spawning a new one per open. */
function _gridViewSelected() {
  const videos = _gridSelectionVideos(_gridSelected.values());
  if (!videos.length) return;
  if (typeof window.openMultiVideos === 'function') {
    window.openMultiVideos(videos, `Selected clips (${videos.length})`);
  }
}

// ── Init (wired by events.html via the Grid tab) ────────────────────────────

// Called from switchTab('grid') in dashboard-events.js the first time the tab
// is opened, then on every subsequent open (cheap; warm server cache).
export function gridInit() {
  if (!_gridInited) {
    _gridInited = true;
    const fromEl = document.getElementById('grid-from');
    const toEl = document.getElementById('grid-to');
    const bucketEl = document.getElementById('grid-bucket');
    const modeEl = document.getElementById('grid-mode');
    // Each mode has a sensible default column width: footage rows are short
    // (≤5 min) so 1-min columns spread the clips across time; detections span a
    // whole night so 5-min columns keep it compact.
    const applyModeBucket = () => { if (bucketEl) bucketEl.value = _gridMode === 'footage' ? '1' : '5'; };
    if (fromEl) fromEl.addEventListener('change', gridLoad);
    if (toEl) toEl.addEventListener('change', gridLoad);
    // From / To: type a time straight in on desktop ("2333"), or use the
    // shared hour/minute wheels (phones, or ↓ in the field).
    const gridOpenPicker = (which, extra = {}) => openTimePicker({
      anchor: which === 'end' ? toEl : fromEl, start: fromEl.value, end: toEl.value, focus: which,
      limits: _gridWin,
      onSet: (st, en) => { fromEl.value = st; toEl.value = en; gridLoad(); },
      ...extra,
    });
    for (const [which, el] of [['start', fromEl], ['end', toEl]]) {
      if (el) wireTimeField(el, { which, openPicker: gridOpenPicker });
    }
    if (bucketEl) bucketEl.addEventListener('change', gridLoad);

    // Thumb-size slider — drives --grid-thumb-w on :root (same mechanism as the
    // events-card zoom, which is known to apply reliably); no re-fetch needed.
    const sizeEl = document.getElementById('grid-size');
    if (sizeEl) {
      const applySize = () => {
        document.documentElement.style.setProperty('--grid-thumb-w', `${sizeEl.value}px`);
      };
      applySize();
      sizeEl.addEventListener('input', applySize);
    }

    // "Other stations" toggle button — reveals cameras outside Romania/Moldova
    // (Berlin). Client-side filter, so just re-render the cached grid.
    const otherBtn = document.getElementById('grid-toggle-other');
    if (otherBtn) {
      otherBtn.classList.toggle('active', _gridShowOther);
      otherBtn.setAttribute('aria-pressed', String(_gridShowOther));
      otherBtn.addEventListener('click', () => {
        _gridShowOther = !_gridShowOther;
        otherBtn.classList.toggle('active', _gridShowOther);
        otherBtn.setAttribute('aria-pressed', String(_gridShowOther));
        if (_gridData) renderGrid(_gridData);
      });
    }
    const camClear = document.getElementById('grid-cam-filter-clear');
    if (camClear) camClear.addEventListener('click', _gridClearCamFilter);
    _gridUpdateCamChip();
    if (modeEl) {
      // A map-point link opens on the footage grid, whatever mode was last used.
      if (_gridCamFilter) modeEl.value = 'footage';
      _gridMode = modeEl.value || _gridMode;
      applyModeBucket();
      modeEl.addEventListener('change', () => {
        _gridMode = modeEl.value; applyModeBucket(); gridSyncHiddenToggle(); gridLoad();
        reportTabView('/events', `grid-${_gridMode}`);   // footage vs detections mode
      });
    }

    // Multi-select toolbar (issue #896): a "Select" toggle plus "View selected"
    // / "Clear". They act on the persisted selection set; the actual per-thumb
    // toggle happens inside the delegated grid click below, gated on select
    // mode. Wired once — the buttons live in the static toolbar, not the
    // re-rendered grid body.
    const selectToggle = document.getElementById('grid-select-toggle');
    if (selectToggle) {
      selectToggle.addEventListener('click', () => _gridSetSelectMode(!_gridSelectMode));
    }
    const viewSelected = document.getElementById('grid-view-selected');
    if (viewSelected) viewSelected.addEventListener('click', _gridViewSelected);
    const clearSelected = document.getElementById('grid-clear-selected');
    if (clearSelected) clearSelected.addEventListener('click', _gridClearSelection);
    _gridUpdateSelectUI();

    // Delegated click for grid cells: attached once on the stable #grid-main
    // container so it survives innerHTML re-renders without leaking listeners.
    // Reading raw values from dataset avoids interpolating station data into
    // an inline handler (XSS-safe).
    const gridMain = document.getElementById('grid-main');
    if (gridMain) {
      // Error events do not bubble, so this listens in the capture phase.
      gridMain.addEventListener('error', (e) => _gridThumbImgFailed(e.target), true);
      gridMain.addEventListener('click', (e) => {
        const cell = e.target.closest('.grid-thumb');
        if (!cell || !gridMain.contains(cell)) return;
        // Hide / Unhide on a detection thumb (also in select mode, and on a
        // pending detection that has no clip yet).
        const hideBtn = e.target.closest('.hd-btn');
        if (hideBtn) {
          e.stopPropagation();
          toggleHide(hideTarget({ meteor_time: cell.dataset.mtime }, cell.dataset.cam),
            hideBtn.dataset.hd === 'hide',
            resp => (window.evOnHiddenChange || gridOnHiddenChange)(resp), { page: 'grid' });
          return;
        }
        // Resolve the whole dataset in one place: any thumb that carries a
        // station key + clip opens, regardless of which station it belongs to
        // (Romania, Moldova, or an "Other stations" camera). Only genuinely
        // pending / hostless cells return null and no-op.
        const clip = _gridResolveThumb(cell.dataset);
        if (!clip) return;
        // In select mode, a click toggles the clip in/out of the selection set
        // instead of opening it. Outside select mode the click plays the single
        // clip as before.
        if (_gridSelectMode) {
          _gridToggleSelectClip(clip);
          return;
        }
        if (typeof window.openSingleVideo === 'function') {
          window.openSingleVideo(clip.host, clip.cam, clip.date, clip.fn, clip.offset, clip.mtime,
            { processed: clip.reencoded, locked: clip.locked, lockType: clip.lockType,
              hidden: cell.dataset.hidden === '1' });
        }
      });
      // Enter / Space on a focused thumb act as a click (open, or toggle in
      // select mode).
      gridMain.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter' && e.key !== ' ') return;
        const cell = e.target.closest?.('.grid-thumb[role="button"]');
        if (!cell || cell !== e.target) return;
        e.preventDefault();
        cell.click();
      });
    }
  }
  gridLoad();
}

// Reload when the shared Night selector changes while the Grid tab is active.
export function gridOnNightChange() {
  if (_gridInited && document.getElementById('pane-grid')?.classList.contains('active')) {
    gridLoad();
  }
}

// Expose for inline handlers / the events.js tab switcher.
/** A hide or unhide (dashboard-hidden.js) on the events page: mark the
 *  loaded grid's cells and re-render it. */
export function gridOnHiddenChange(resp) {
  if (!_gridData || _gridMode === 'footage') return;
  let n = 0;
  for (const row of _gridData.rows || []) {
    for (const [cam, cells] of Object.entries(row.cells || {})) n += markAll(cells, resp, cam);
  }
  if (!n) return;
  if (resp?.changed) {
    _gridData.hidden_count = Math.max(0, (_gridData.hidden_count || 0) + (resp.hidden ? 1 : -1));
  }
  renderGrid(_gridData);
  gridSyncHiddenToggle();
  window.evRestoreScroll?.();
}

/** The Grid toolbar's "Show hidden": shown in "Detections only" (footage
 *  lists every chunk, so there is nothing to show), with the night's count. */
export function gridSyncHiddenToggle() {
  const slot = document.getElementById('grid-hidden-slot');
  const btn = document.getElementById('grid-hidden-toggle');
  if (!slot) return;
  slot.hidden = !btn || _gridMode === 'footage';
  if (btn && _gridMode !== 'footage') updateToggle(btn, _gridData?.hidden_count || 0);
}

window.gridInit = gridInit;
window.gridOnHiddenChange = gridOnHiddenChange;
window.gridSyncHiddenToggle = gridSyncHiddenToggle;
window.gridReload = () => { if (_gridData) gridLoad(); };
window.gridOnNightChange = gridOnNightChange;
