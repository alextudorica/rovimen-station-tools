import { state, escHtml, openAppPanel, appPanelClose } from './dashboard-common.js';

// toggleLogoMenu / openAppPanel / appPanelClose / _APP_PANELS now live in
// dashboard-common.js. Inline copies removed to avoid "Identifier
// '_APP_PANELS' has already been declared" SyntaxError.

// Hide header when embedded in admin iframe
if (window.self !== window.top) {
  document.getElementById('cfg-header').style.display = 'none';
}

// ---------------------------------------------------------------------------
// Config field definitions
// Each field: { label, path: array of keys into config JSON, type: 'bool'|'number'|'string' }
// ---------------------------------------------------------------------------
const SECTIONS = [
  { name: 'Services', fields: [
    { label: 'stacker.enabled',      path: ['services','stacker','enabled'],         type: 'bool' },
    { label: 'stacker.realtime',     path: ['services','stacker','realtime'],        type: 'bool' },
    { label: 'stacker.nice',         path: ['services','stacker','nice'],            type: 'number', min:-20, max:19,  step:1 },
    { label: 'stacker.cpu_quota',    path: ['services','stacker','cpu_quota'],       type: 'number', min:5,   max:200, step:5 },
    { label: 'reencode.enabled',     path: ['services','reencode','enabled'],        type: 'bool' },
    { label: 'detection_lock',       path: ['services','detection_lock','enabled'],  type: 'bool' },
    { label: 'archive_upload',       path: ['services','archive_upload','enabled'],  type: 'bool' },
    { label: 'timelapse_build',      path: ['services','timelapse_build','enabled'], type: 'bool' },
  ]},
  { name: 'Location', fields: [
    { label: 'latitude',             path: ['latitude'],                    type: 'number', min:-90,   max:90,   step:0.001 },
    { label: 'longitude',            path: ['longitude'],                   type: 'number', min:-180,  max:180,  step:0.001 },
    { label: 'elevation (m)',        path: ['elevation'],                   type: 'number', min:0,     max:5000, step:1 },
  ]},
  { name: 'Capture', fields: [
    { label: 'segment_duration',     path: ['segment_duration'],            type: 'number', min:5,   max:120, step:5 },
    { label: 'rtsp_capture_delay_s', path: ['rtsp_capture_delay_s'],        type: 'number', min:0,   max:30,  step:1 },
    { label: 'ff_idle_timeout_min',  path: ['ff_idle_timeout_minutes'],     type: 'number', min:5,   max:120, step:5 },
    { label: 'min_disk_gb_free',     path: ['min_disk_gb_free'],            type: 'number', min:1,   max:100, step:1 },
    { label: 'encode_workers',       path: ['encode_workers'],              type: 'number', min:1,   max:8,   step:1 },
    { label: 'compression_level',    path: ['compression_level'],           type: 'number', min:0,   max:4,   step:1 },
  ]},
  { name: 'Detection', fields: [
    { label: 'pre_seconds',          path: ['detection','pre_seconds'],     type: 'number', min:0,   max:30,  step:1 },
    { label: 'post_seconds',         path: ['detection','post_seconds'],    type: 'number', min:0,   max:60,  step:1 },
  ]},
  { name: 'Retention (days)', fields: [
    { label: 'color_days',           path: ['retention','color_days'],      type: 'number', min:1,   max:30,  step:1 },
    { label: 'locked_days',          path: ['retention','locked_days'],     type: 'number', min:1,   max:90,  step:1 },
    { label: 'stacks_days',          path: ['retention','stacks_days'],     type: 'number', min:1,   max:90,  step:1 },
    { label: 'timelapse_days',       path: ['retention','timelapse_days'],  type: 'number', min:1,   max:90,  step:1 },
  ]},
  { name: 'Disk Thresholds (%)', fields: [
    { label: 'warn_pct',             path: ['disk','warn_pct'],             type: 'number', min:50,  max:95,  step:1 },
    { label: 'nuclear_pct',          path: ['disk','nuclear_pct'],          type: 'number', min:60,  max:98,  step:1 },
    { label: 'extreme_pct',          path: ['disk','extreme_pct'],          type: 'number', min:70,  max:99,  step:1 },
  ]},
  { name: 'Archive', fields: [
    { label: 'enabled',              path: ['archive','enabled'],           type: 'bool' },
    { label: 'host',                 path: ['archive','host'],              type: 'string' },
    { label: 'upload_meteors',       path: ['archive','upload_meteors'],    type: 'bool' },
    { label: 'upload_timelapses',    path: ['archive','upload_timelapses'], type: 'bool' },
    { label: 'upload_stacks',        path: ['archive','upload_stacks'],     type: 'bool' },
    { label: 'interval_minutes',     path: ['archive','interval_minutes'],  type: 'number', min:5, max:120, step:5 },
  ]},
  { name: 'Overlay', fields: [
    { label: 'enabled',              path: ['overlay','enabled'],           type: 'bool' },
    { label: 'style',                path: ['overlay','style'],             type: 'enum', options: ['standard','cinema'] },
    { label: 'network',              path: ['overlay','network'],           type: 'string' },
    { label: 'coords',               path: ['overlay','coords'],            type: 'string' },
    { label: 'font_size',            path: ['overlay','font_size'],         type: 'number', min:8,   max:48,  step:1 },
    { label: 'text_opacity',         path: ['overlay','text_opacity'],      type: 'number', min:0,   max:1,   step:0.05 },
    { label: 'bar_logo_opacity',     path: ['overlay','bar_logo_opacity'],  type: 'number', min:0,   max:1,   step:0.05 },
    { label: 'show_logo',            path: ['overlay','show_logo'],         type: 'bool' },
    { label: 'show_network',         path: ['overlay','show_network'],      type: 'bool' },
    { label: 'show_timestamp',       path: ['overlay','show_timestamp'],    type: 'bool' },
    { label: 'show_station',         path: ['overlay','show_station'],      type: 'bool' },
    { label: 'show_coords',          path: ['overlay','show_coords'],       type: 'bool' },
    { label: 'show_pointing',        path: ['overlay','show_pointing'],     type: 'bool' },
  ]},
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function getPath(obj, path) {
  let cur = obj;
  for (const k of path) {
    if (cur == null || typeof cur !== 'object') return undefined;
    cur = cur[k];
  }
  return cur;
}

function consensus(values) {
  // Most common value among non-undefined entries
  const counts = new Map();
  for (const v of values) {
    if (v === undefined) continue;
    const k = JSON.stringify(v);
    counts.set(k, (counts.get(k) || 0) + 1);
  }
  if (!counts.size) return undefined;
  let best = null, bestCount = 0;
  for (const [k, c] of counts) {
    if (c > bestCount) { bestCount = c; best = k; }
  }
  return JSON.parse(best);
}

function formatVal(val, type) {
  if (val === undefined) return { text: '—', cls: 'missing' };
  if (type === 'bool') {
    return { text: '', cls: val ? 'bool-true val-bool-true' : 'bool-false val-bool-false' };
  }
  return { text: String(val), cls: 'ok' };
}

// ---------------------------------------------------------------------------
// Cell rendering (read-only)
//
// The matrix used to offer in-place editing that PATCHed each station's
// /api/settings. Stations are read-only from the dashboard (2026-07
// hardening): the settings PATCH proxy is gone, so the matrix is now a pure
// comparison/diff view — config changes are applied over SSH or shipped as
// signed bundles.
// ---------------------------------------------------------------------------

function renderCell(val, field, hostKey, si, fi) {
  // hostKey is admin-managed (dashboard_config.yaml). si/fi are local indices
  // but escape the lot for consistency in attribute context.
  const tdAttrs = `data-section="${si}" data-fi="${fi}" data-hk="${escHtml(hostKey)}"`;
  const { text, cls } = formatVal(val, field.type);
  return `<td class="val ${cls}" ${tdAttrs}>${escHtml(text)}</td>`;
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------

function renderMatrix(stations, stationMeta, configs) {
  const onlineStations = stations.filter(hk => configs[hk]?.ok);
  let totalDiffs = 0;

  const statusBar = document.getElementById('cfg-status-bar');
  statusBar.innerHTML = stations.map(hk => {
    const r = configs[hk];
    const label = stationMeta[hk]?.label || hk;
    const cls = !r ? 'offline' : r.ok ? 'online' : 'error';
    const title = r?.error ? ` title="${escHtml(r.error)}"` : '';
    return `<span class="cfg-badge ${cls}"${title}><span translate="no">${escHtml(hk)}</span> · ${escHtml(label)}</span>`;
  }).join('');

  const sections = SECTIONS.map((section, si) => {
    let sectionDiffs = 0;

    const diffFields = section.fields.map((field, fi) => {
      const vals = stations.map(hk => {
        const r = configs[hk];
        if (!r || !r.ok) return undefined;
        return getPath(r.config, field.path);
      });
      const con = consensus(vals);
      const hasDiff = onlineStations.length > 1 &&
        onlineStations.some(hk => {
          const v = getPath(configs[hk].config, field.path);
          return v !== undefined && JSON.stringify(v) !== JSON.stringify(con);
        });
      if (hasDiff) sectionDiffs++;
      return hasDiff;
    });
    totalDiffs += sectionDiffs;

    const headerCells = section.fields.map((field, fi) => {
      const diffMark = diffFields[fi] ? `<span style="color:var(--yellow);font-size:9px;margin-left:2px" role="img" aria-label="(stations differ)" title="Stations differ">▲</span>` : '';
      return `<th scope="col" style="text-align:center;font-size:11px;white-space:nowrap" translate="no">${escHtml(field.label)}${diffMark}</th>`;
    }).join('');

    const rows = stations.map(hk => {
      const r = configs[hk];
      const label = stationMeta[hk]?.label || '';
      const cells = section.fields.map((field, fi) => {
        const val = r?.ok ? getPath(r.config, field.path) : undefined;
        return renderCell(val, field, hk, si, fi);
      }).join('');
      return `<tr>
        <td class="station-name"><span translate="no">${escHtml(hk)}</span><span class="st-lbl">${escHtml(label)}</span></td>
        ${cells}
      </tr>`;
    }).join('');

    const diffBadge = sectionDiffs > 0
      ? `<span class="diff-count">${sectionDiffs} diff${sectionDiffs > 1 ? 's' : ''}</span>` : '';

    return `<div class="cfg-section">
      <button type="button" class="cfg-section-hdr" aria-expanded="true" onclick="toggleSection(this)">
        <span>${escHtml(section.name)}</span>${diffBadge}
        <span class="chevron" aria-hidden="true">▾</span>
      </button>
      <div class="cfg-section-body">
        <div style="overflow-x:auto">
        <table class="cfg-table">
          <thead><tr>
            <th scope="col" style="position:sticky;left:0;background:rgba(48,54,61,.5);z-index:2">Station</th>
            ${headerCells}
          </tr></thead>
          <tbody>${rows}</tbody>
        </table></div>
      </div>
    </div>`;
  }).join('');

  const banner = document.getElementById('cfg-diff-banner');
  if (totalDiffs > 0) {
    banner.style.display = 'block';
    banner.textContent = `${totalDiffs} inconsistenc${totalDiffs > 1 ? 'ies' : 'y'} detected across station configs.`;
  } else {
    banner.style.display = 'none';
  }

  document.getElementById('cfg-body').innerHTML = sections;
}

function toggleSection(hdr) {
  const body = hdr.nextElementSibling;
  const collapsed = body.classList.toggle('hidden');
  hdr.classList.toggle('collapsed', collapsed);
  hdr.setAttribute('aria-expanded', String(!collapsed));
}

// ---------------------------------------------------------------------------
// Load
// ---------------------------------------------------------------------------

async function loadAndRender() {
  try {
    const [configsResp, stationsResp] = await Promise.all([
      fetch('/api/config/all'),
      fetch('/api/stations'),
    ]);

    if (configsResp.status === 401) {
      document.getElementById('cfg-body').innerHTML =
        '<div class="cfg-error">Login required to view configs. <a href="/login">Log in</a></div>';
      return;
    }

    const configs = await configsResp.json();
    const stationMeta = await stationsResp.json();

    const stations = Object.keys(stationMeta);
    renderMatrix(stations, stationMeta, configs);
  } catch(e) {
    document.getElementById('cfg-body').innerHTML =
      `<div class="cfg-error">Failed to load: ${escHtml(e.message)}</div>`;
  }
}

loadAndRender();

// ---------------------------------------------------------------------------
// Window exposure for onclick handlers in HTML and dynamic template literals
// ---------------------------------------------------------------------------
window.toggleSection = toggleSection;
window.openAppPanel = openAppPanel;
window.appPanelClose = appPanelClose;
