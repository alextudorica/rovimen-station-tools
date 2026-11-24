/*
 * dashboard-home.js
 * ─────────────────
 * The home page "/" (overview.html, ov_page 'home'): the station and camera
 * counts, the network numbers, the latest plots per camera (stacks grid,
 * plot-type toggle, card-size slider) and the plot zoom modal. The Hall of
 * Fame on this page is dashboard-hof.js.
 *
 * The Network overview "/network" runs dashboard-overview.js instead (map,
 * live view, GMN orbits, status table); neither page loads the other's
 * module. The top bar both pages share is drawn by renderOverviewSummary and
 * loadOverviewNetStats in dashboard-common.js.
 */
import { escHtml, fetchJson, fetchJsonResponse, openAppPanel, appPanelClose, _modalOpen, _modalClose, fetchAuthStatus, setPressed, renderOverviewSummary, loadOverviewNetStats } from './dashboard-common.js';
import { VideoModal } from './dashboard-video-modal.js';

// The Hall of Fame clip modal shows operator-only controls off
// state.AUTH_USER, which the page's one session request writes; anonymous
// visitors keep {user:null} -> playback-only.
fetchAuthStatus();
// The browser back gesture closes open video modals through this hook
// (_closeAllOpenModals in dashboard-common.js).
window._VideoModalCloseAll = () => VideoModal.closeAll();

// Build skeleton HTML — one shimmering card per camera across all online
// stations, grouped by host so the cold-start layout previews the real grid.
// Only used by fetchAndRenderStacks on its first call (retryCount === 0).
function _ovBuildSkeletons(overviewData){
  let html = '';
  for (const [host, info] of Object.entries(overviewData || {})) {
    if (!info.online) continue;
    const cams = info.cameras || [];
    if (!cams.length) continue;
    let row = '';
    for (let i = 0; i < cams.length; i++) {
      row += `<div class="ov-stack-skeleton" aria-hidden="true">
        <div class="ov-stack-skeleton-img"></div>
        <div class="ov-stack-skeleton-meta"></div>
      </div>`;
    }
    html += `<div class="ov-stack-skeleton-group" aria-hidden="true">
      <div class="ov-stack-skeleton-label"></div>
      <div class="ov-stack-skeleton-row">${row}</div>
    </div>`;
  }
  return html;
}

/* Camera 180° rotation map keyed by ``host__cam``. Populated whenever
   fetchAndRenderStacks runs from overviewData.cameras[].rotate. RMS plot
   images (FF native sensor orientation) are now rotated client-side via
   the ``.thumb-rotated`` CSS class instead of via a server-side PIL pass.
   Station/archive colour stacks are pre-rotated upstream so they never
   get the class. The colour meteor stack filename is excluded for the
   same reason. See dashboard.css. */
const COLOR_METEOR_STACK_FILENAME = '__color_meteor_stack__.webp';

/* Colour the per-card date label by how recent the captured night is,
   so an offline-for-days station is visually obvious in the stacks grid.
   `dateYmd` is the upstream YYYYMMDD captured-night date (= the *start*
   of the night, so 20260521 = night of May 21→22 UTC). "Last night" =
   today UTC − 1 day → green; one day older → yellow; older still → red. */
function _ovDateAgeClass(dateYmd) {
  if (!dateYmd || dateYmd.length !== 8) return '';
  const y = +dateYmd.slice(0,4), m = +dateYmd.slice(4,6), d = +dateYmd.slice(6,8);
  const captured = Date.UTC(y, m - 1, d);
  const now = new Date();
  const todayUtc = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  const days = Math.round((todayUtc - captured) / 86400000);
  if (days <= 1) return 'fresh';   // last completed night
  if (days === 2) return 'stale';  // one extra day old
  return 'old';
}

let _rotateByCam = {};
function _ovBuildRotateMap(overviewData) {
  const m = {};
  for (const [host, info] of Object.entries(overviewData || {})) {
    for (const c of (info.cameras || [])) {
      if (c && c.code) m[`${host}__${c.code}`] = !!c.rotate;
    }
  }
  return m;
}
function _ovCameraRotates(host, cam) {
  return !!_rotateByCam[`${host}__${cam}`];
}
function _ovImgUrlNeedsRotation(host, cam, imgUrl) {
  if (!_ovCameraRotates(host, cam)) return false;
  // The `rotate` flag describes the camera's *sensor* orientation. Only
  // raw frames inherit it — FF captured stacks, max/avepixel. Matplotlib
  // charts (radiants, fieldsums, astrometry/calibration variation,
  // FF intervals, observing periods) carry axis labels that must stay
  // upright. The colour meteor stack is pre-rotated upstream by the
  // station stacker, so it never qualifies either.
  try {
    const u = new URL(imgUrl, location.href);
    const parts = u.pathname.split('/');
    const i = parts.indexOf('plot_image');
    if (i < 0 || parts.length < i + 5) return false;
    const fn = decodeURIComponent(parts[i + 4]);
    if (fn === COLOR_METEOR_STACK_FILENAME) return false;
    // The per-night composite (colour-only cameras' fallback tile) is built
    // from station-side stacks that are already rotated upstream.
    if (/_night_stack\./i.test(fn)) return false;
    return /_(stack|maxpixel|avepixel)\./i.test(fn);
  } catch (e) {
    return false;
  }
}

async function fetchAndRenderStacks(overviewData, retryCount = 0) {
  const grid = document.getElementById('ov-stacks-grid');
  _rotateByCam = _ovBuildRotateMap(overviewData);
  // On the very first call (cold start), paint skeleton placeholders so the
  // stacks section is never blank while /api/overview/stacks is in flight.
  // Subsequent retries leave the existing "Loading stacks…" message in place.
  if (retryCount === 0 && grid) {
    const skeletons = _ovBuildSkeletons(overviewData);
    if (skeletons) grid.innerHTML = skeletons;
  }
  try {
    const res = await fetchJsonResponse('/api/overview/stacks');
    // The backend sets X-Stacks-Warming while its fan-out is still in flight.
    // Retrying on a *non-empty* warming snapshot is what keeps a partially
    // covered fleet from rendering as permanent "no data" tiles; an empty
    // response is only the cold-start case, not the only bad one.
    const warming = res.headers.get('X-Stacks-Warming') === '1';
    const stacks = await res.json();
    // Cold dashboard restart or worker recycle — backend cache empty or still
    // filling. Retry a few times with backoff before giving up.
    const willRetry = (!stacks.length || warming) && retryCount < 6;
    if (willRetry) {
      const delay = [500, 1500, 3000, 5000, 8000, 12000][retryCount];
      setTimeout(() => fetchAndRenderStacks(overviewData, retryCount + 1), delay);
    }
    if (!stacks.length) {
      grid.innerHTML = willRetry
        ? `<div class="ov-stacks-loading">Loading stacks (cache warming, retry ${retryCount + 1}/6)…</div>`
        : '<div class="ov-stacks-loading">No captured stacks available</div>';
      return;
    }
    // Non-empty while warming still renders immediately, so the grid fills in
    // progressively; the scheduled retry replaces it with the full fleet.
    // Group by host
    const byHost = {};
    for (const s of stacks) {
      (byHost[s.host] = byHost[s.host] || []).push(s);
    }
    // Also include offline stations (no stacks) for completeness
    // Track cameras that have stacks so we can fetch their plot lists
    const camPlotTargets = [];
    let html = '';
    // Longest card first: each station's cameras sit on one row, and the
    // cards pack side by side by that length (dashboard-overview.css).
    const camCount = ([h, i]) => Math.max((byHost[h] || []).length, (i.cameras || []).length);
    const ordered = Object.entries(overviewData).sort((a, b) => camCount(b) - camCount(a));
    for (const [host, info] of ordered) {
      const online = info.online;
      const cams = byHost[host] || [];
      const offlineCams = (info.cameras || []).filter(c => !cams.find(s => s.cam === c.code));
      if (!cams.length && !offlineCams.length) continue;

      html += `<div class="ov-station-group">
        <a class="ov-station-label" href="/station/${encodeURIComponent(host)}" title="Open ${escHtml(info.label || host)}"
           style="text-decoration:none">
          <span class="dot ${online ? 'up' : 'down'}"></span>
          ${escHtml(host)} <span class="ov-station-loc">${escHtml(info.label)}</span>
        </a>
        <div class="ov-cam-grid">`;

      // Cameras with stacks — card without per-camera button row.
      // Plot switching is driven by the global toggle bar above the grid.
      // Click opens fullscreen modal; the small "↗" badge navigates to
      // the per-station detail page.
      for (const s of cams) {
        const imgUrl = `/api/rms/plot_image/${encodeURIComponent(s.host)}/${encodeURIComponent(s.cam)}/${encodeURIComponent(s.date)}/${encodeURIComponent(s.filename)}`;
        const dateStr = String(s.date || '').replace(/(\d{4})(\d{2})(\d{2})/, '$1-$2-$3');
        const ageCls = _ovDateAgeClass(s.date);
        const imgId = `ov-cam-img-${s.host}-${s.cam}`;
        // Captured stacks are FF-derived (native sensor orientation). Apply
        // the 180° client-side rotation class when the camera's rotate flag
        // is set; the colour meteor stack filename never qualifies (it's
        // already station-rotated upstream).
        const rotCls = _ovImgUrlNeedsRotation(s.host, s.cam, imgUrl) ? ' class="thumb-rotated"' : '';
        // JSON.stringify gives a properly JS-escaped string literal; escHtml
        // then makes it safe to embed inside an HTML attribute value.
        const onclickArgs = `${escHtml(JSON.stringify(imgId))},${escHtml(JSON.stringify(s.host))},${escHtml(JSON.stringify(s.cam))},${escHtml(JSON.stringify(dateStr))}`;
        html += `<div class="ov-cam-card-wrap">
          <div class="ov-cam-card" role="button" tabindex="0"
               title="${escHtml(s.cam)} — ${escHtml(dateStr)} (click to expand)"
               onclick="ovOpenCardImg(${onclickArgs})"
               onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();ovOpenCardImg(${onclickArgs})}">
            <a class="ov-cam-jump" href="/station/${encodeURIComponent(s.host)}/videodb?cam=${encodeURIComponent(s.cam)}&date=${encodeURIComponent(s.date || '')}"
               title="Open ${escHtml(s.cam)} in the Video DB on this night" onclick="event.stopPropagation()">↗</a>
            <img id="${escHtml(imgId)}"${rotCls} src="${escHtml(imgUrl)}" data-default-src="${escHtml(imgUrl)}" data-host="${escHtml(s.host)}" data-cam="${escHtml(s.cam)}" alt="${escHtml(s.cam)}" loading="lazy" decoding="async"
                 onerror="this.style.opacity='0.3';this.onerror=null">
            <div class="ov-cam-code">${escHtml(s.cam)} <span class="ov-cam-date ${ageCls}">${escHtml(dateStr)}</span></div>
          </div>
        </div>`;
        camPlotTargets.push({ host: s.host, cam: s.cam, date: s.date, imgId });
      }

      // Cameras without stacks (offline or no data)
      for (const c of offlineCams) {
        html += `<div class="ov-cam-card-wrap">
          <a href="/station/${encodeURIComponent(host)}/videodb?cam=${encodeURIComponent(c.code)}" class="ov-cam-card no-stack" title="${escHtml(c.code)} — no data">
            <img src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='120'%3E%3C/svg%3E" alt="${escHtml(c.code)}" decoding="async">
            <div class="ov-cam-code">${escHtml(c.code)} <span class="ov-cam-date">no data</span></div>
          </a>
        </div>`;
      }

      html += '</div></div>';
    }
    grid.innerHTML = html;

    // Fetch every camera's plot list in parallel, then build ONE global
    // toggle row from the union of labels, rendered above the stacks grid.
    // PRE-POPULATE plotsByCam with an empty entry for every imgId so failed
    // fetches still appear in Object.entries() — otherwise the lazy refresh
    // can't see them and they stay frozen forever.
    const plotsByCam = {};  // imgId -> { label -> url }
    for(const t of camPlotTargets) plotsByCam[t.imgId] = {};
    // plots-latest resolves the newest ARCHIVED night that actually carries
    // plots, so a camera whose last night failed to process still shows its
    // previous plots (marked stale) instead of a blank card, and it works
    // with the station offline. Its `date` is the plot night, which is not
    // necessarily the stack night in `t.date` — build image URLs from the
    // returned date, never from t.date.
    const latest = await ovFetchLatestPlots(camPlotTargets);
    for (const t of camPlotTargets) {
      const res = latest.get(`${t.host}/${t.cam}`);
      const plots = (res && res.plots) || [];
      if (!plots.length) continue;
      const m = {};
      for (const p of plots) {
        m[p.label] = `/api/rms/plot_image/${t.host}/${t.cam}/${res.date}/${encodeURIComponent(p.filename)}`;
      }
      plotsByCam[t.imgId] = m;
      ovMarkPlotStaleness(t.imgId, res);
    }
    renderOverviewPlotToggle(plotsByCam);
    // Cards whose plot-list fetch failed have empty {} entries here. Schedule
    // aggressive early retries (3s, 8s, 20s) in addition to the 60s
    // setInterval — the dashboard-side cache typically warms within a few
    // seconds of restart, but during that window the first JS fetch may
    // race ahead of the dashboard's own prefetch loop.
    [3000, 8000, 20000].forEach(ms =>
      setTimeout(() => { try { ovRefreshMissingPlotLists(); } catch(e) {} }, ms)
    );
  } catch (e) {
    grid.innerHTML = '<div class="ov-stacks-loading">Failed to load stacks; reload the page to retry.</div>';
  }
}

/** Newest archived RMS plots for many cameras: one batched request
 *  (/api/rms/plots-latest?cams=host/CAM,...) instead of one per camera, which
 *  was about 40 requests on every visit to the map. Falls back to the
 *  per-camera route if the batch call fails. Resolves to a Map keyed
 *  "host/CAM"; cameras without an answer are absent. */
export async function ovFetchLatestPlots(targets, fetchFn = fetch) {
  const out = new Map();
  const keys = [...new Set(targets.map(t => `${t.host}/${t.cam}`))];
  if (!keys.length) return out;
  try {
    const r = await fetchFn(`/api/rms/plots-latest?cams=${keys.map(encodeURIComponent).join(',')}`);
    if (!r.ok) throw new Error(String(r.status));
    const data = await r.json();
    for (const k of keys) if (data && data[k]) out.set(k, data[k]);
    return out;
  } catch (e) {
    await Promise.all(keys.map(async k => {
      try {
        const r = await fetchFn(`/api/rms/plots-latest/${k}`);
        if (r.ok) out.set(k, await r.json());
      } catch (e2) {}
    }));
    return out;
  }
}

// Short labels keyed by the server's label string. Order here controls the
// displayed order of buttons in the global toggle bar.
// Strict whitelist — only these plot labels render as toggle buttons on the
// overview. Anything else returned by /api/rms/plots is ignored. Order here
// controls the displayed order.
const OV_PLOT_LABEL_ORDER = [
  'Captured stack', 'Meteor stack', 'Radiants', 'Field sums',
  'Astrometry calibration', 'Calibration variation',
  'Photometry calibration', 'Photometry variation',
  'FF intervals', 'Observing periods',
];
const OV_PLOT_SHORT = {
  'Captured stack':          'All stack',
  'Meteor stack':            'Meteor stack',
  'Radiants':                'Radiants',
  'Field sums':              'Fieldsums',
  'Astrometry calibration':  'Astrometry',
  'Calibration variation':   'Calib var',
  'Photometry calibration':  'Photometry',
  'Photometry variation':    'Phot var',
  'FF intervals':            'FF intervals',
  'Observing periods':       'Obs periods',
};

// `plotsByCam` is { imgId: { label: url } }. Render one button per unique
// label that exists on at least one camera. Clicking a button swaps EVERY
// camera card's thumbnail to that plot (cameras missing that plot keep
// their default stack so the grid never goes blank).
function renderOverviewPlotToggle(plotsByCam){
  const bar = document.getElementById('ov-plot-toggle-bar');
  if(!bar) return;
  const union = new Set();
  for(const m of Object.values(plotsByCam)) for(const lbl of Object.keys(m)) union.add(lbl);
  // Strict whitelist: only render buttons for labels in OV_PLOT_LABEL_ORDER,
  // in that exact order. Anything else the API returns (thumbs, detected,
  // photometry, etc.) is intentionally hidden.
  const ordered = OV_PLOT_LABEL_ORDER.filter(l => union.has(l));
  if(!ordered.length){ bar.style.display = 'none'; return; }
  const plotsByCamJson = JSON.stringify(plotsByCam).replace(/"/g, '&quot;');
  bar.dataset.plotsByCam = plotsByCamJson;
  const buttons = ordered.map(lbl => {
    const short = OV_PLOT_SHORT[lbl] || lbl;
    const active = lbl === 'Captured stack' ? ' active' : '';
    return `<button type="button" class="ov-plot-toggle-btn${active}" data-label="${lbl}" aria-pressed="${active ? 'true' : 'false'}"
              onclick="ovGlobalPlotSwitch(this,'${lbl.replace(/'/g,"\\'")}')">${short}</button>`;
  }).join('');
  bar.innerHTML = `<span class="ov-plot-toggle-bar-label">Show plot:</span>${buttons}`;
  bar.style.display = 'flex';
}

// Camera card width (image 4:3 in CSS); a station's cameras wrap onto more
// rows when they no longer fit side by side.
function ovUpdateZoom(val){
  const px = parseInt(val, 10) || 160;
  document.documentElement.style.setProperty('--ov-card-w', px + 'px');
}

/* Inline SVG placeholder rendered on cards that don't have the selected
   plot type (e.g. clicking 'Meteor stack' on a station with zero meteors).
   Without this, those cards keep showing their captured_stack thumbnail
   and the toggle looks broken. */
function _ovNoDataTile(label){
  const t = (label || 'plot').replace(/[<>&"]/g, '');
  const svg = `<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 160 120'>
    <rect width='160' height='120' fill='#161b22'/>
    <text x='50%' y='46%' fill='#6e7681' font-family='monospace' font-size='10'
          text-anchor='middle' dominant-baseline='middle'>no data</text>
    <text x='50%' y='62%' fill='#484f58' font-family='monospace' font-size='9'
          text-anchor='middle' dominant-baseline='middle'>${t}</text>
  </svg>`;
  return 'data:image/svg+xml;utf8,' + encodeURIComponent(svg);
}

function ovGlobalPlotSwitch(btn, label){
  const bar = document.getElementById('ov-plot-toggle-bar');
  if(!bar) return;
  bar.querySelectorAll('.ov-plot-toggle-btn').forEach(b => setPressed(b, b === btn));
  bar.dataset.activeLabel = label;
  const noDataTile = _ovNoDataTile(label);
  let plotsByCam = {};
  try {
    plotsByCam = JSON.parse((bar.dataset.plotsByCam || '{}').replace(/&quot;/g, '"'));
  } catch(e) {}
  for(const [imgId, byLabel] of Object.entries(plotsByCam)){
    const img = document.getElementById(imgId);
    if(!img) continue;
    const url = byLabel[label];
    if(url){
      img.src = url;
      img.dataset.hasPlot = '1';
    } else if(label === 'Captured stack'){
      // Captured stack is the default; every camera has it (it's how they
      // ended up in the grid in the first place). Restore the default src.
      img.src = img.dataset.defaultSrc || img.src;
      img.dataset.hasPlot = '1';
    } else {
      // No data for this plot type on this camera — show the placeholder.
      img.src = noDataTile;
      img.dataset.hasPlot = '0';
    }
    // Re-evaluate the rotation class against the new src. The inline SVG
    // no-data placeholder never qualifies (it's not a station image).
    const h = img.dataset.host || '';
    const cam = img.dataset.cam || '';
    if (_ovImgUrlNeedsRotation(h, cam, img.src)) img.classList.add('thumb-rotated');
    else img.classList.remove('thumb-rotated');
  }
  // Lazy-refetch plot lists for any cards whose plotsByCam entry is empty
  // or stale — this typically happens when /api/rms/plots calls failed
  // during the initial fetchAndRenderStacks pass (cold cache, slow station).
  ovRefreshMissingPlotLists();
}

/* Open the currently-displayed thumbnail in the fullscreen modal.
   Pulls live src from the <img> so whatever plot is on screen — captured
   stack, meteor stack, radiants, etc. — is what zooms. The .thumb-rotated
   class is propagated to the modal img by ovPlotOpen so the 180° flip
   stays consistent between card and fullscreen view. */
function ovOpenCardImg(imgId, host, cam, dateStr){
  const img = document.getElementById(imgId);
  if(!img) return;
  const bar = document.getElementById('ov-plot-toggle-bar');
  const lbl = (bar && bar.dataset.activeLabel) || 'Captured stack';
  ovPlotOpen(img.src, `${cam} · ${dateStr} · ${lbl}`, _ovImgUrlNeedsRotation(host, cam, img.src));
}

/* Badge a camera card whose displayed plots come from an older night than
   the one that should have been processed by now.

   Without this the fallback is invisible: the card shows a plot and looks
   current, whether it is last night's or three weeks old. Yellow for stale,
   red once the expected night is properly overdue — matching the
   fresh/stale/old convention already used for the stack date. */
function ovMarkPlotStaleness(imgId, res){
  const img = document.getElementById(imgId);
  if(!img || !res) return;
  const card = img.closest('.ov-cam-card');
  if(!card) return;
  card.querySelector('.ov-plot-stale')?.remove();
  // Remember it so a toggle-bar re-render can restore the badge.
  img.dataset.plotDate = res.date || '';
  img.dataset.plotStale = res.stale ? '1' : '0';
  img.dataset.plotOverdue = res.overdue ? '1' : '0';
  if(!res.stale) return;
  const badge = document.createElement('span');
  badge.className = 'ov-plot-stale' + (res.overdue ? ' overdue' : '');
  badge.textContent = `plots ${res.date}`;
  badge.title = res.overdue
    ? `Newest plots are from ${res.date}; ${res.expected_date} is overdue`
    : `Newest plots are from ${res.date}, not ${res.expected_date}`;
  card.appendChild(badge);
}

/* Re-fetch /api/rms/plots for cards whose plotsByCam entry is empty or
   missing the labels we know exist on at least one camera. Patches the
   stored plotsByCam in-place and reapplies the active label. Cheap on
   warm cache; safe to call periodically. */
async function ovRefreshMissingPlotLists(){
  const bar = document.getElementById('ov-plot-toggle-bar');
  if(!bar) return;
  let plotsByCam = {};
  try {
    plotsByCam = JSON.parse((bar.dataset.plotsByCam || '{}').replace(/&quot;/g, '"'));
  } catch(e) { return; }
  const targets = [];
  for(const [imgId, byLabel] of Object.entries(plotsByCam)){
    if(!byLabel || !Object.keys(byLabel).length){
      const img = document.getElementById(imgId);
      if(!img) continue;
      // The img's default src is /api/rms/plot_image/<host>/<cam>/<date>/<file>
      // — parse it to recover host, cam, date for the refetch.
      const u = new URL(img.dataset.defaultSrc || img.src, location.href);
      const parts = u.pathname.split('/');
      // /api/rms/plot_image/<host>/<cam>/<date>/<file>
      const i = parts.indexOf('plot_image');
      if(i < 0 || parts.length < i + 4) continue;
      targets.push({ imgId, host: parts[i+1], cam: parts[i+2], date: parts[i+3] });
    }
  }
  if(!targets.length) return;
  let mutated = false;
  const latest = await ovFetchLatestPlots(targets);
  for (const t of targets) {
    const res = latest.get(`${t.host}/${t.cam}`);
    const plots = (res && res.plots) || [];
    const m = {};
    for(const p of plots){
      m[p.label] = `/api/rms/plot_image/${t.host}/${t.cam}/${res.date}/${encodeURIComponent(p.filename)}`;
    }
    if(Object.keys(m).length){
      plotsByCam[t.imgId] = m;
      ovMarkPlotStaleness(t.imgId, res);
      mutated = true;
    }
  }
  if(mutated){
    bar.dataset.plotsByCam = JSON.stringify(plotsByCam).replace(/"/g, '&quot;');
    // Re-render the toggle bar — if the initial fetch left every entry
    // empty (all stations cold-cached) the bar was hidden by
    // renderOverviewPlotToggle. Now that lazy refresh has filled in
    // labels, redraw it. Preserve the currently-active label so the
    // user's selection survives the redraw.
    const prevActive = bar.querySelector('.ov-plot-toggle-btn.active');
    const prevLabel = prevActive ? prevActive.dataset.label : (bar.dataset.activeLabel || 'Captured stack');
    renderOverviewPlotToggle(plotsByCam);
    // Restore active state on the matching button (default is 'Captured stack')
    if(prevLabel && prevLabel !== 'Captured stack'){
      bar.querySelectorAll('.ov-plot-toggle-btn').forEach(b => {
        if(b.dataset.label === prevLabel){
          bar.querySelectorAll('.ov-plot-toggle-btn').forEach(a => setPressed(a, a === b));
        }
      });
    }
    // Reapply the active label to swap thumbnails for newly-fetched cameras
    const noDataTile = _ovNoDataTile(prevLabel);
    for(const [imgId, byLabel] of Object.entries(plotsByCam)){
      const img = document.getElementById(imgId);
      if(!img) continue;
      const url = byLabel[prevLabel];
      if(url){ img.src = url; }
      else if(prevLabel === 'Captured stack'){ img.src = img.dataset.defaultSrc || img.src; }
      else { img.src = noDataTile; }
      const h = img.dataset.host || '';
      const cam = img.dataset.cam || '';
      if (_ovImgUrlNeedsRotation(h, cam, img.src)) img.classList.add('thumb-rotated');
      else img.classList.remove('thumb-rotated');
    }
  }
}

(async function startHome(){
  try{
    const data=await fetchJson('/api/overview');
    const has=id=>!!document.getElementById(id);
    if(has('ov-summary')) renderOverviewSummary(data);
    // Stacks don't block paint: they fire in the background and self-render
    // once data arrives.
    if(has('ov-stacks-grid')) fetchAndRenderStacks(data);
    // The network numbers read the server's platepar_store, which the
    // stations' pushed platepars keep current (#1248), so they load at once.
    if(has('ov-net-stats')) loadOverviewNetStats();
    // Overview refresh every 30 s. Server cache TTL is 60 s anyway and the
    // response carries ETag/Cache-Control: max-age=30, so a shorter interval
    // just wastes round-trips. Also pause when tab hidden.
    setInterval(async()=>{
      if(document.hidden) return;
      try{ const d=await fetchJson('/api/overview'); if(has('ov-summary')) renderOverviewSummary(d); }catch(e){}
    },30000);
    // Re-attempt plot-list fetches every 60s so cards whose initial fetch
    // missed (cold cache, slow station) eventually fill in. Cheap on the
    // warm dashboard-side cache.
    setInterval(()=>{
      if(document.hidden) return;
      try{ ovRefreshMissingPlotLists(); }catch(e){}
    }, 60000);
    // On tab refocus, catch up the paused pollers so the user doesn't stare
    // at up-to-30-s-stale data while the next setInterval tick is pending.
    document.addEventListener('visibilitychange', ()=>{
      if(document.hidden) return;
      (async()=>{ try{ const d=await fetchJson('/api/overview'); if(has('ov-summary')) renderOverviewSummary(d); }catch(e){} })();
      try{ ovRefreshMissingPlotLists(); }catch(e){}
    });
  }catch(e){
    const sum=document.getElementById('ov-summary');
    if(sum) sum.innerHTML='<div class="ov-summary-loading" style="color:var(--red)">Failed to load station data; reload the page to retry.</div>';
  }
})();

const _panelParam = new URLSearchParams(location.search).get('panel');
if (_panelParam) openAppPanel(_panelParam);

/* ── Overview plot modal with scroll-zoom ── */
let _ovPz = { scale: 1, tx: 0, ty: 0 };
/* When the card was 180°-rotated via the .thumb-rotated class, mirror that
   onto the modal img and bake rotate(180deg) into every inline transform
   the scroll-zoom handlers write (otherwise the inline style overrides the
   class and the modal flips to right-side-up mid-zoom). Place rotate(...)
   as the rightmost / first-applied op so user pan operates in screen
   coordinates rather than the rotated image frame. */
function _ovTransformSuffix(img) {
  return img.classList.contains('thumb-rotated') ? ' rotate(180deg)' : '';
}
function ovPlotOpen(imgUrl, label, rotated = false) {
  const modal = document.getElementById('ov-plot-modal');
  const img = document.getElementById('ov-plot-modal-img');
  document.getElementById('ov-plot-modal-title').textContent = label;
  img.src = imgUrl;
  document.getElementById('ov-plot-modal-dl').href = imgUrl;
  _ovPz = { scale: 1, tx: 0, ty: 0 };
  if (rotated) img.classList.add('thumb-rotated');
  else img.classList.remove('thumb-rotated');
  img.style.transform = rotated ? 'rotate(180deg)' : '';
  img.style.cursor = 'zoom-in';
  // _modalOpen handles the body-scroll lock now (in dashboard-common.js).
  _modalOpen(modal);
}
function ovPlotClose() {
  const img = document.getElementById('ov-plot-modal-img');
  _modalClose(document.getElementById('ov-plot-modal'));
  img.src = '';
  img.classList.remove('thumb-rotated');
  img.style.transform = '';
}
// Escape closes this page's own dialogs, topmost first. A shared VideoModal
// (Hall of Fame clips) open on top keeps the key to itself.
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape' || e.defaultPrevented) return;
  if (document.querySelector('.vm-backdrop.open, .vm-backdrop[aria-hidden="false"]')) return;
  const isOpen = id => document.getElementById(id)?.classList.contains('open');
  if (isOpen('ov-plot-modal')) ovPlotClose();
  else if (isOpen('app-panel')) appPanelClose(null);
  else return;
  e.preventDefault();
});
(function() {
  const img = document.getElementById('ov-plot-modal-img');
  img.addEventListener('wheel', e => {
    e.preventDefault();
    _ovPz.scale = Math.max(1, Math.min(10, _ovPz.scale * (e.deltaY < 0 ? 1.2 : 0.83)));
    if (_ovPz.scale <= 1.05) { _ovPz = { scale: 1, tx: 0, ty: 0 }; }
    img.style.transform = `scale(${_ovPz.scale})${_ovTransformSuffix(img)}`;
    img.style.cursor = _ovPz.scale > 1 ? 'grab' : 'zoom-in';
  }, { passive: false });
  let dragging = false, sx = 0, sy = 0;
  img.addEventListener('mousedown', e => {
    if (_ovPz.scale <= 1) return;
    dragging = true; sx = e.clientX - _ovPz.tx; sy = e.clientY - _ovPz.ty;
    img.style.cursor = 'grabbing';
  });
  document.addEventListener('mousemove', e => {
    if (!dragging) return;
    _ovPz.tx = e.clientX - sx; _ovPz.ty = e.clientY - sy;
    img.style.transform = `translate(${_ovPz.tx}px,${_ovPz.ty}px) scale(${_ovPz.scale})${_ovTransformSuffix(img)}`;
  });
  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    img.style.cursor = _ovPz.scale > 1 ? 'grab' : 'zoom-in';
  });
})();

// ── Window exposure for HTML onclick/oninput handlers ────────────────────────
// Functions called from inline event handlers in the HTML template (and the
// cards rendered above) must be on window: module scope is not visible to
// HTML attributes.
window.ovPlotClose = ovPlotClose;
window.ovPlotOpen = ovPlotOpen;
window.ovGlobalPlotSwitch = ovGlobalPlotSwitch;
window.ovOpenCardImg = ovOpenCardImg;
window.openAppPanel = openAppPanel;
window.appPanelClose = appPanelClose;
window.ovUpdateZoom = ovUpdateZoom;
