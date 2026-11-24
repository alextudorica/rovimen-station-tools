import { _modalOpen, _modalClose, escHtml, serviceIsUp, state, nightTitle, rmsNightOf } from './dashboard-common.js';
/*
 * dashboard-sysadmin.js
 * ─────────────────────
 * The station page's System Admin tab (#pane-settings in dashboard.html), all
 * of it: the Health pane (night timeline, system graphs, storage and janitor,
 * services, scheduled jobs, recent events), the read-only Configuration pane
 * (hardware suggestions, who can see the station, overlay logo and preview,
 * software status), the Logs pane and the Live view pane.
 *
 * An ES module, loaded by dashboard.html before dashboard-station.js; they
 * share nothing at module level and talk through window. The station page
 * owns tab switching, polling and the status/settings fetches, and calls
 * renderSysAdmin, renderSysAdminIfChanged and refreshNightIfShown. This module
 * calls the station page's mkCameraWindows, mkServices, mkStorage,
 * _ownsStation, fetchOnce, _isAbort, fetchStatus and fetchSettings, and reads
 * the data it fills in (window.settingsData, hwData, encStatsData,
 * vitalsHistory, coreData, cronsData, storagewatchData). The tab's inline
 * handlers call the window.* functions at the end of this file.
 */

/* ─────────────────────────────────────────
   System Admin tab render
───────────────────────────────────────── */
let activeSysAdminTab = {};  // host -> 'health' | 'processes' | 'updatelogs' | 'livestream'
let nightState        = {};  // host -> { date: 'YYYYMMDD' }

function renderSuggestions(host, cfg) {
  const hw = window.hwData[host];
  if (!hw || hw.__missing) {
    return `<section class="sa-card"><h2>Hardware</h2>
      <p class="sa-note">No hardware profile yet. Run <span class="sa-mono">bash rovimen_probe.sh</span> on the station to make one.</p>
    </section>`;
  }

  const ret = cfg.retention || {};
  const sug = hw.suggested  || {};
  // Stations are read-only from the dashboard (2026-07 hardening): these are
  // advice, applied over SSH.
  const suggestions = [];
  const workers = cfg.encode_workers ?? 1;
  if (sug.encode_workers && sug.encode_workers !== workers) {
    suggestions.push(`Encoder workers <span class="sa-mono">${workers}</span> to <span class="sa-mono sa-strong">${escHtml(sug.encode_workers)}</span>, for a ${escHtml(hw.cpu_cores)}-core CPU.`);
  }
  const colorDays = ret.color_days ?? 2;
  if (sug.retention_color_days && Math.abs(sug.retention_color_days - colorDays) >= 1) {
    suggestions.push(`Unlocked clip retention <span class="sa-mono">${colorDays}</span> to <span class="sa-mono sa-strong">${escHtml(sug.retention_color_days)}</span> days, for a ${escHtml(hw.disk_gb)} GB disk.`);
  }
  if (hw.vaapi_h264_encode && !(cfg.capabilities || {}).vaapi && sug.compression_level !== undefined
      && (cfg.compression_level ?? 0) === 0) {
    suggestions.push('Compression from raw copy to <span class="sa-mono sa-strong">QP 20</span>: this station can encode on VAAPI.');
  }

  const cpu = hw.cpu_model ? escHtml(hw.cpu_model.replace(/\(R\)|\(TM\)/g, '').replace(/\s+/g, ' ').trim()) : '?';
  const vaapi = hw.vaapi_h264_encode ? `VAAPI H.264 (${escHtml(hw.vaapi_driver || '?')})`
    : hw.vaapi ? 'VAAPI without H.264 encode' : 'No VAAPI';
  const profile = `<p class="sa-note">${cpu}, ${escHtml(hw.cpu_cores)} cores, ${escHtml(hw.ram_gb)} GB RAM,
    ${escHtml(hw.disk_gb)} GB disk, ${vaapi}${hw.probed_at ? `. Probed ${escHtml(hw.probed_at.slice(0, 10))}` : ''}.</p>`;

  if (!suggestions.length) {
    return `<section class="sa-card"><div class="sa-head"><h2>Hardware</h2><span class="sa-note">Settings fit this hardware</span></div>${profile}</section>`;
  }
  return `<section class="sa-card sa-suggest">
    <h2>${suggestions.length === 1 ? '1 suggested change' : `${suggestions.length} suggested changes`}</h2>
    ${profile}
    <ul class="sa-sug">${suggestions.map(t => `<li><span>${t}</span><span class="sa-note">Apply over SSH</span></li>`).join('')}</ul>
  </section>`;
}

function _mkDataVisCard(host) {
  const meta = state.STATIONS_META[host] || {};
  const pub  = new Set(meta.public_tabs || []);
  const row  = (tab, label, desc) => `
    <div class="sa-switch-row">
      <label for="sa-vis-${tab}-${host}"><span class="sa-strong">${label}</span><span class="sa-note">${desc}</span></label>
      <span class="sa-switch"><input type="checkbox" id="sa-vis-${tab}-${host}" ${pub.has(tab) ? 'checked' : ''}
             onchange="saveDataVisibility('${host}','${tab}',this.checked,this)"><span aria-hidden="true"></span></span>
    </div>`;
  return `<section class="sa-card sa-group" id="sa-c-vis-${host}" aria-labelledby="sa-c-vis-h-${host}">
    <div class="sa-stack"><h2 id="sa-c-vis-h-${host}">Who can see this station</h2>
      <p class="sa-note">Guests and owners of other stations. Saved at once.</p></div>
    <div>
      ${row('timelapse', 'Timelapses', 'Nightly timelapse videos')}
      ${row('videodb',   'Video DB',   'The colour clip browser')}
      ${row('archive',   'Archive',    'Clips in cloud storage')}
    </div>
  </section>`;
}

async function saveDataVisibility(host, tab, enabled, input = null) {
  const meta = state.STATIONS_META[host] || {};
  const current = new Set(meta.public_tabs || []);
  if (enabled) current.add(tab); else current.delete(tab);
  const r = await fetch(`/api/admin/network-config/station/${host}`, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({public_tabs: [...current]}),
  }).catch(() => null);
  // A switch that stays flipped after a failed save would claim a change
  // that never happened.
  if (!r || !r.ok) { if (input) input.checked = !enabled; return; }
  if (!state.STATIONS_META[host]) state.STATIONS_META[host] = {};
  state.STATIONS_META[host].public_tabs = [...current];
}

// Tracks which (host, cfg-fingerprint) pair currently owns the #pane-settings
// DOM so renderSysAdmin can skip the ~200-element rebuild on every tab switch
// (T2). Cleared by refreshSysAdmin, fetchSettings, and any save handler that
// rewrites cfg — those still need a full rebuild because the static HTML
// reflects cfg values via spinners/inputs.
const _settingsRenderState = {};

// Fingerprint the data baked into the static HTML by renderSysAdmin. Cheap
// regions (vitals, services list, crons, storage rows, dawn progress, RMS
// status, updater status, logs) are NOT included — they refresh via their
// own updater functions and don't trigger a full rebuild. status.services
// 'color-capture' IS included because the rrovimen-disabled banner and the
// color-capture svc-block border depend on it.
function _sysAdminCfgSig(host) {
  const cfg = window.settingsData[host];
  if (!cfg) return null;
  const status = state.statusData[host] || {};
  return JSON.stringify({
    cfg,                       // every form value rides on this
    hw:  window.hwData[host] || null, // capabilities (vaapi/hwencode)
    enc: window.encStatsData[host]?.overall || null,  // header stats box
    cc:  (status.services || {})['color-capture'] || null, // affects banner
    sl:  !!state.statusData[host],   // statusLoaded gate
  });
}

// Refresh only the data-driven sub-regions of #pane-settings without
// rebuilding the form structure. Called on tab re-entry when the fingerprint
// matches and the pane is already wired up.
function _refreshSettingsRegions(host) {
  const status = state.statusData[host] || {};
  // Services list
  const sec = document.getElementById(`sec-services-sysadmin-${host}`);
  if (sec) sec.innerHTML = window.mkServices(status.services || {});
  // Storage card
  const stEl = document.getElementById(`storage-data-${host}`);
  if (stEl) stEl.innerHTML = window.mkStorage(status.storage, status.disk, status.extra_disks, status.disk_pressure, status.capture_fallback, window.settingsData[host]?.disk);
  // Async region refreshers — same calls the full render makes post-mount
  fetchCrons(host);
  fetchStoragewatchStatus(host);
  fetchStationEvents(host);
  fetchUpdaterStatus(host);
}

// The status poll's hook: rebuild when data baked into the tab (config,
// hardware, colour capture on or off) changed since this host was rendered.
function renderSysAdminIfChanged(host) {
  if (_settingsRenderState[host] && _sysAdminCfgSig(host) !== _settingsRenderState[host].sig) renderSysAdmin(host);
}

function renderSysAdmin(host) {
  const el  = document.getElementById('pane-settings');

  if (!state.IS_ADMIN) {
    if (document.getElementById(`sysadmin-host-${host}`)) return;
    const cams = window.mkCameraWindows(host);
    let body;
    if (cams) {
      body = cams;
    } else if (!window._ownsStation(host)) {
      body = '<div class="offline">Live feed is only available to this station’s operator.</div>';
    } else {
      body = '<div class="offline">No cameras configured for this station</div>';
    }
    el.innerHTML = `<div id="sysadmin-host-${host}">${body}</div>`;
    return;
  }

  const cfg = window.settingsData[host];
  if (!cfg) { el.innerHTML = '<div class="offline">Loading…</div>'; return; }
  if (cfg.__error) { el.innerHTML = `<div class="offline">Failed — ${escHtml(cfg.__error)}</div>`; return; }

  const sig = _sysAdminCfgSig(host);
  const prev = _settingsRenderState[host];
  const marker = document.getElementById(`sysadmin-root-${host}`);
  if (prev && prev.sig === sig && marker) {
    _refreshSettingsRegions(host);
    switchSysAdminTab(host, activeSysAdminTab[host] || 'health');
    return;
  }

  const status = state.statusData[host] || {};
  const statusLoaded = !!state.statusData[host];
  const rrovimenOn = !statusLoaded || status.online === false || serviceIsUp((status.services || {})['color-capture']);

  const rrovimenBanner = rrovimenOn ? '' :
    `<div class="sa-alert sa-alert-warn"><strong>Colour capture is off.</strong> Stacking, encoding, detection lock and everything that depends on them are not running.</div>`;

  const saTab = (key, label) =>
    `<button type="button" id="sa-tab-${key}-${host}" role="tab" aria-selected="false"
             tabindex="-1" aria-controls="sa-pane-${key}-${host}"
             onclick="switchSysAdminTab('${host}','${key}')">${label}</button>`;
  const topBar = `<div class="sa-bar">
    <div class="sa-seg" role="tablist" aria-label="Station Admin sections">
      ${saTab('health', 'Health')}
      ${saTab('processes', 'Configuration')}
      ${saTab('updatelogs', 'Logs')}
      ${saTab('livestream', 'Live view')}
    </div>
    <button type="button" class="sa-btn" onclick="refreshSysAdmin('${host}', this)">Refresh</button>
  </div>`;
  const saPane = (key, html) =>
    `<div id="sa-pane-${key}-${host}" class="sa-pane" role="tabpanel" aria-labelledby="sa-tab-${key}-${host}">${html}</div>`;

  const healthPane     = saPane('health', _saHealthHtml(host, status, cfg));
  const processesPane  = saPane('processes', _saConfigHtml(host, cfg, rrovimenOn));
  const updatelogPane  = saPane('updatelogs', `<div id="logs-section-${host}"><span class="sa-note">Loading…</span></div>`);
  const livestreamPane = saPane('livestream', window.mkCameraWindows(host));

  // Wrap the rendered output in a marker div so subsequent renderSysAdmin
  // calls can detect "pane is wired for this host" and take the incremental
  // path (T2). Clearing window.settingsData[host] or calling refreshSysAdmin both
  // invalidate the cache via _settingsRenderState below.
  el.innerHTML = `<div id="sysadmin-root-${host}" class="sa-root">`
    + [topBar, rrovimenBanner, healthPane, processesPane, updatelogPane, livestreamPane].join('')
    + `</div>`;
  _settingsRenderState[host] = { sig };

  // Post-render: activate the remembered tab (or default to health)
  switchSysAdminTab(host, activeSysAdminTab[host] || 'health');

  fetchCrons(host);
  fetchStoragewatchStatus(host);
  fetchStationEvents(host);
  fetchUpdaterStatus(host);
  logoRefreshList(host);
}

/* ── Health ─────────────────────────────── */
function _saHealthHtml(host, status, cfg) {
  const vital = (k, label) => `<div class="sa-vital">
      <span class="sa-vital-label">${label}</span>
      <span class="sa-vital-val sa-mono" id="vital-${k}-${host}">—</span>
      <canvas id="graph-${k}-${host}" class="sa-spark" height="34" aria-hidden="true"></canvas>
    </div>`;
  return `
    <section class="sa-card sa-night" aria-labelledby="night-h-${host}">
      <div class="sa-head">
        <div class="sa-stack">
          <h2 id="night-h-${host}">Night</h2>
          <p class="sa-note" id="night-sum-${host}">Loading…</p>
          <p class="sa-note" id="night-asof-${host}"></p>
        </div>
        <div class="sa-nav">
          <button type="button" class="sa-btn" aria-label="Previous night" onclick="nightNavDate('${host}',-1)">‹</button>
          <span class="sa-mono" id="night-date-${host}">—</span>
          <button type="button" class="sa-btn" id="night-next-${host}" aria-label="Next night" onclick="nightNavDate('${host}',1)">›</button>
        </div>
      </div>
      <div class="sa-tl"><div class="sa-tl-grid" id="night-tl-${host}"></div></div>
      <div class="sa-legend">
        <span><i class="sa-key sa-key-cap"></i>Colour capture</span>
        <span class="sa-legend-stage" hidden><i class="sa-key sa-key-proc"></i>Morning processing</span>
        <span class="sa-legend-stage" hidden><i class="sa-key sa-key-up"></i>Archive upload</span>
        <span><i class="sa-key sa-key-gap"></i>No colour video for 2 min or more</span>
        <span class="sa-legend-now"><i class="sa-key sa-key-now"></i>Now</span>
      </div>
    </section>
    <div class="sa-row2">
      <section class="sa-card sa-grow3" aria-labelledby="vit-h-${host}">
        <div class="sa-head"><h2 id="vit-h-${host}">System</h2><span class="sa-note">Last 5 minutes</span></div>
        <div class="sa-vitals">
          ${vital('cpu', 'CPU load')}${vital('temp', 'Temperature')}${vital('ram', 'Memory')}${vital('disk', 'Disk write')}
        </div>
        <div class="sa-cols">
          <div class="sa-stack"><h3>Cores</h3><div id="graph-cores-${host}" class="sa-bars"><span class="sa-note">Waiting for data…</span></div></div>
          <div class="sa-stack"><h3>Top processes</h3><div id="graph-top-procs-${host}" class="sa-bars"></div></div>
        </div>
      </section>
      <section class="sa-card sa-grow2" aria-labelledby="sto-h-${host}">
        <h2 id="sto-h-${host}">Storage</h2>
        <div id="storage-data-${host}">${window.mkStorage(status.storage, status.disk, status.extra_disks, status.disk_pressure, status.capture_fallback, cfg.disk)}</div>
        <div id="janitor-section-${host}"><span class="sa-note">Janitor: loading…</span></div>
      </section>
    </div>
    <section class="sa-card" aria-labelledby="svc-h-${host}">
      <h2 id="svc-h-${host}">Services and scheduled jobs</h2>
      <div class="sa-cols">
        <div class="sa-stack"><h3>Services</h3><div id="sec-services-sysadmin-${host}">${window.mkServices(status.services || {})}</div></div>
        <div class="sa-stack"><h3>Scheduled jobs</h3><div id="crons-table-${host}"><span class="sa-note">Loading…</span></div></div>
      </div>
    </section>
    <section class="sa-card" id="events-card-${host}" aria-labelledby="evt-h-${host}" hidden>
      <div class="sa-head"><h2 id="evt-h-${host}">Recent events</h2><span class="sa-note">Boots, uplink changes, offline and online</span></div>
      <div id="events-list-${host}"><span class="sa-note">Loading…</span></div>
    </section>`;
}

/* ── Configuration (read-only apart from the overlay logo: stations take config over SSH) ── */
function _saConfigHtml(host, cfg, rrovimenOn) {
  const ov  = cfg.overlay   || {};
  const ret = cfg.retention || {};
  const svc = cfg.services  || {};
  const arc = cfg.archive   || {};
  const det = cfg.detection || {};
  const dsk = cfg.disk      || {};
  const e = v => escHtml(String(v ?? '—'));
  const onOff = v => v ? 'On' : 'Off';
  const kv = rows => rows.length ? `<dl class="sa-kv">${rows.map(([k, v, u]) =>
    `<dt>${k}</dt><dd><span class="sa-mono">${e(v)}</span>${u ? ` <span class="sa-unit">${u}</span>` : ''}</dd>`).join('')}</dl>` : '';
  const group = (id, name, desc, state, rows, extra = '') => `
    <section class="sa-card sa-group" id="sa-c-${id}-${host}" aria-labelledby="sa-c-${id}-h-${host}">
      <div class="sa-head">
        <div class="sa-stack"><h2 id="sa-c-${id}-h-${host}">${name}</h2><p class="sa-note">${desc}</p></div>
        <span class="sa-pill${state === 'Off' ? '' : ' on'}">${state}</span>
      </div>
      ${kv(rows)}${extra}
    </section>`;

  const useVaapi = cfg.capabilities?.vaapi ?? true;
  // [VAAPI name, CPU name, what it costs]
  const levels = [['Raw copy', 'Raw copy', 'No recompression'], ['QP 19', 'CRF 20', 'Visually lossless'],
                  ['QP 20', 'CRF 21', 'Small loss, good balance'], ['QP 21', 'CRF 22', 'Depends on the sky'],
                  ['QP 22', 'CRF 23', 'Clear, dark nights only']];
  const lvl = cfg.compression_level ?? 0;
  const st = window.encStatsData[host];
  const o = st?.overall;
  const encStat = o && o.chunks
    ? `<p class="sa-note">Last night ${o.chunks} chunks shrank by <span class="sa-mono sa-strong">${o.avg}%</span> on average
        (median ${o.median}%, ${o.min ?? '—'} to ${o.max ?? '—'}%)${o.size_avg != null ? `, ${o.size_avg} MB a chunk` : ''}${st.raw_mb ? ` against ${st.raw_mb} MB raw` : ''}.</p>`
    : '';
  const compression = `<div class="sa-stack">
      <span class="sa-note">Compression, lighter to stronger</span>
      <ol class="sa-scale">${levels.map((l, i) => `<li${i === lvl ? ' aria-current="true"' : ''}>
        <span class="sa-mono">${l[useVaapi ? 0 : 1]}</span><span>${l[2]}</span></li>`).join('')}</ol>
      ${encStat}
    </div>`;

  const shown = [['show_logo', 'Logo'], ['show_network', 'Network'], ['show_station', 'Station'],
                 ['show_coords', 'Coords'], ['show_pointing', 'Pointing'], ['show_timestamp', 'Timestamp']]
    .filter(([k]) => ov[k] !== false).map(([, l]) => l).join(', ') || 'Nothing';
  const logoName = (ov.logo || '').split('/').pop();
  const overlayExtra = `<div class="sa-actions">
      <label class="sa-strong" for="sa-logo-${host}">Logo</label>
      <select id="sa-logo-${host}" class="sa-select" onchange="logoPicked('${host}')">
        <option value="${e(logoName)}">${logoName ? e(logoName) : 'None'}</option>
      </select>
      <button type="button" class="sa-btn" id="sa-logo-save-${host}" disabled
              onclick="saveOverlayLogo('${host}', this)">Save logo</button>
      <span class="sa-note" id="sa-logo-msg-${host}" role="status"></span>
    </div>
    <div class="sa-actions">
      <button type="button" class="sa-btn" onclick="previewOverlay('${host}', this)">Preview overlay</button>
      <span class="sa-note">Renders one frame with these settings and the logo picked above.</span>
    </div>
    <div id="overlay-preview-${host}" hidden><img id="overlay-preview-img-${host}" class="sa-preview" decoding="async" alt="Overlay preview"></div>`;

  const cams = Object.entries(cfg.stations || {});
  const rotated = cams.filter(([, sc]) => sc.rotate).length;

  const groups = [
    ['cap', 'Colour capture', 'Records colour video from every camera. Stacking, encoding, detection lock and archive upload all depend on it.',
      onOff(rrovimenOn), [
        ['Segment length', cfg.segment_duration ?? 20, 'seconds'], ['RTSP start delay', cfg.rtsp_capture_delay_s ?? 6, 'seconds'],
        ['Restart ffmpeg after idle', cfg.ff_idle_timeout_minutes ?? 30, 'minutes'], ['Stop when free space below', cfg.min_disk_gb_free ?? 10, 'GB'],
        ['Disk warning', dsk.warn_pct ?? 85, '%'], ['Aggressive cleanup', dsk.nuclear_pct ?? 90, '%'], ['Emergency cleanup', dsk.extreme_pct ?? 95, '%']]],
    ['stk', 'Stacker', 'Builds max-pixel stacks from colour chunks. In real time it skips chunks it can’t keep up with and pauses while RMS reads frames; the morning sweep fills every gap.',
      onOff(svc.stacker?.enabled ?? true), [
        ['Real-time stacking', onOff(svc.stacker?.realtime ?? true)], ['Nice level', svc.stacker?.nice ?? 10, '−20 to 19'],
        ['CPU quota', svc.stacker?.cpu_quota ?? 50, '%']]],
    ['enc', 'Encoder', 'Re-encodes colour chunks with the overlay burned in.',
      onOff(svc.reencode?.enabled ?? true), [
        ['Hardware encoding', (cfg.capabilities?.hwencode ?? true) ? 'VAAPI H.264' : 'Off, libx264'],
        ['Workers', cfg.encode_workers ?? 1, 'in parallel']], compression],
    ['lock', 'Detection lock', 'Keeps colour chunks that contain an RMS meteor detection.',
      onOff(svc.detection_lock?.enabled ?? true), [
        ['Before detection', det.pre_seconds ?? 3, 'seconds'], ['After detection', det.post_seconds ?? 12, 'seconds'],
        ['Keep unlocked clips', ret.color_days ?? 2, 'days'], ['Keep locked clips', ret.locked_days ?? 7, 'days'],
        ['Keep stacks', ret.stacks_days ?? 7, 'days'], ['Keep timelapses', ret.timelapse_days ?? 7, 'days']]],
    ['arc', 'Archive upload', 'Copies meteors and timelapses to the storage box.',
      onOff(svc.archive_upload?.enabled ?? true), [
        ['Meteors', (arc.upload_meteors ?? true) ? 'Upload' : 'Don’t upload'],
        ['Timelapses', (arc.upload_timelapses ?? true) ? 'Upload' : 'Don’t upload'],
        ['Stacks', (arc.upload_stacks ?? false) ? 'Upload' : 'Don’t upload'],
        ['Host', arc.host || '—'], ['Port', arc.port ?? 22], ['User', arc.user ?? 'root'],
        ['Base path', arc.base_path || '—'], ['Every', arc.interval_minutes ?? 20, 'minutes']]],
    ['tl', 'Timelapse', 'Assembles the nightly timelapse from the stacked frames.',
      onOff(svc.timelapse_build?.enabled ?? true), []],
    ['ov', 'Overlay', 'Text and logo burned into encoded video.',
      onOff(ov.enabled !== false), [
        ['Style', ov.style === 'cinema' ? 'Cinema' : 'Standard'], ['Network name', ov.network ?? 'ROVIMEN'],
        ['Coordinates', ov.coords || '—'], ['Font size', ov.font_size ?? 19, 'px'],
        ['Text opacity', ov.text_opacity ?? 0.8], ['Logo opacity', ov.bar_logo_opacity ?? 0.9],
        ['Shown', shown]], overlayExtra],
    ['rot', 'Camera rotation', 'Flips the picture 180° for cameras mounted upside down.',
      `${rotated} of ${cams.length}`, cams.map(([code, sc]) => [escHtml(code), sc.rotate ? 'Rotated 180°' : 'Normal'])],
  ];

  const index = [...groups.map(([id, name]) => [id, name]), ['vis', 'Who can see this station'], ['upd', 'Software']]
    .map(([id, name]) => `<button type="button" onclick="document.getElementById('sa-c-${id}-${host}')?.scrollIntoView({behavior:'smooth',block:'start'})">${name}</button>`).join('');

  return `<div class="sa-config">
    <nav class="sa-index" aria-label="Configuration sections">${index}</nav>
    <div class="sa-config-body">
      <div class="sa-card sa-readonly">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>
        <div class="sa-stack">
          <span class="sa-strong">This is what the station is running. Only the overlay logo can be changed from the dashboard.</span>
          <span class="sa-note">Change the rest on the station over SSH with <span class="sa-mono">config_patch.py</span>, then mirror it into <span class="sa-mono">station_configs/</span>. The overlay logo and “Who can see this station” are saved from here.</span>
        </div>
      </div>
      ${renderSuggestions(host, cfg)}
      ${groups.map(g => group(...g)).join('')}
      ${_mkDataVisCard(host)}
      <section class="sa-card sa-group" id="sa-c-upd-${host}" aria-labelledby="sa-c-upd-h-${host}">
        <div class="sa-head"><h2 id="sa-c-upd-h-${host}">Software</h2><span id="upd-status-badge-${host}" class="sa-note"></span></div>
        <dl class="sa-kv">
          <dt>Channel</dt><dd class="sa-mono">${e(cfg.update_channel || 'main')}</dd>
          <dt>Installed</dt><dd class="sa-mono" id="upd-local-${host}">—</dd>
          <dt>Latest bundle</dt><dd class="sa-mono" id="upd-remote-${host}">—</dd>
        </dl>
        <p class="sa-note">Signed bundles install automatically. Channel changes and forced updates need SSH.</p>
      </section>
      <details class="sa-raw">
        <summary>Show config.json</summary>
        <pre class="sa-mono">${escHtml(JSON.stringify(cfg, null, 2))}</pre>
      </details>
    </div>
  </div>`;
}

function switchSysAdminTab(host, tab) {
  activeSysAdminTab[host] = tab;
  ['health', 'processes', 'updatelogs', 'livestream'].forEach(p => {
    const pane = document.getElementById(`sa-pane-${p}-${host}`);
    const btn  = document.getElementById(`sa-tab-${p}-${host}`);
    if (pane) pane.hidden = p !== tab;
    if (btn) {
      btn.setAttribute('aria-selected', String(p === tab));
      btn.tabIndex = (p === tab) ? 0 : -1;
    }
  });
  if (tab === 'health') {
    setTimeout(() => drawAllGraphs(host), 0);
    fetchNight(host);
  }
  if (tab === 'updatelogs') { renderLogsShell(); fetchLogs(); }
}

/* ─────────────────────────────────────────
   Night timeline (Health): capture coverage per camera, from the
   state.json each station pushes to the archive
───────────────────────────────────────── */
function _lastNight() {
  return rmsNightOf(new Date());
}

// Gaps shorter than this are a chunk or two lost to a restart, not news.
const NIGHT_GAP_MIN = 2;

// Minutes after noon -> "HH:MM" UTC.
const _nightHHMM = min => window.twilightSlider.sliderValToHHMM(Math.floor(min));

// The status a camera's lane shows, from its archive summary. `old` is a
// night whose data should long have arrived: no state.json then means none
// will come, and an unknown morning_done (a listing cached before that field
// existed) means dawn finished long ago.
export function nightLaneStatus(c, old = false) {
  if (!c) return { text: old ? 'No station data for this night' : 'Not uploaded yet', tone: 'muted' };
  if (!c.total) return { text: 'No colour video', tone: 'bad' };
  if (c.morning_done || (c.morning_done == null && old)) return { text: 'Done', tone: 'muted' };
  if (!c.rms_complete) return { text: 'Waiting for RMS', tone: 'muted' };
  return { text: 'Processing', tone: 'busy' };
}

// What each pipeline step got through, as {label, value, dim, title}. A
// finished night can legitimately stop short: dawn stacks only meteor chunks
// where the stacker scope says so, and encodes only meteor clips when the
// full-night re-encode is off or stopped early. Those counts are dimmed with
// the reason, so they don't read as a fault.
export function nightStageCounts(c) {
  if (!c || !c.total) return [];
  const n = v => Number(v || 0).toLocaleString('en-GB');
  const short = (done, all) => c.morning_done && done < all;
  const out = [
    { label: 'Stacked', value: `${n(c.stacked)}/${n(c.total)}`, dim: short(c.stacked, c.total),
      title: 'Dawn stacked only the meteor chunks this night' },
    { label: 'Encoded', value: `${n(c.reencoded)}/${n(c.total)}`, dim: short(c.reencoded, c.total),
      title: 'Full-night re-encode was off or stopped early; meteor clips are always encoded' },
  ];
  if (c.timelapse_done != null) out.push({ label: 'Timelapse', value: c.timelapse_done ? '✓' : '—', dim: false, title: '' });
  out.push({ label: 'Clips uploaded', value: `${n(c.uploaded)}/${n(c.locked)}`, dim: false, title: '' });
  return out.map(x => ({ ...x, title: x.dim ? x.title : '' }));
}

// The pipeline after capture as bars, from state.json's stage_times
// (minutes after noon): morning processing from when dawn started on the
// night (stage_times.rms_complete: dawn marks RMS complete as it starts,
// on its 15-minute schedule) until morning_done, or until `now` while it
// runs; archive uploads from the first to the latest. Empty without times.
export function nightStageBars(c, now = null) {
  const st = c?.stages || {};
  const bars = [];
  if (st.rms_complete != null) {
    const end = st.morning_done ?? (c.morning_done === false && now != null ? now : null);
    if (end != null && end > st.rms_complete) {
      bars.push({ kind: 'proc', from: st.rms_complete, to: end, running: st.morning_done == null });
    }
  }
  if (st.first_upload != null) {
    bars.push({ kind: 'up', from: st.first_upload, to: Math.max(st.last_upload ?? st.first_upload, st.first_upload) });
  }
  return bars;
}

// Holes between capture spans of at least NIGHT_GAP_MIN, as [start, end].
export function nightGaps(spans) {
  const gaps = [];
  for (let i = 1; i < spans.length; i++) {
    if (spans[i][0] - spans[i - 1][1] >= NIGHT_GAP_MIN) gaps.push([spans[i - 1][1], spans[i][0]]);
  }
  return gaps;
}

async function fetchNight(host) {
  if (!nightState[host]) nightState[host] = { date: _lastNight() };
  const date = nightState[host].date;
  const [r, tw] = await Promise.all([
    window.fetchOnce(`tab:night:${host}`, `/api/station_night/${host}/${date}`).catch(e => e),
    fetch(`/api/twilight/${host}/${date}`).then(x => x.ok ? x.json() : null).catch(() => null),
  ]);
  if (window._isAbort(r)) return;
  if (nightState[host].date !== date) return;
  let data = null;
  if (r instanceof Response && r.ok) data = await r.json().catch(() => null);
  renderNight(host, date, data, tw);
}

function renderNight(host, date, data, tw) {
  const grid = document.getElementById(`night-tl-${host}`);
  if (!grid) return;
  const d = new Date(`${date.slice(0, 4)}-${date.slice(4, 6)}-${date.slice(6, 8)}T12:00:00Z`);
  const title = document.getElementById(`night-h-${host}`);
  if (title) title.textContent = nightTitle(date);
  const lbl = document.getElementById(`night-date-${host}`);
  if (lbl) lbl.textContent = `${date.slice(0, 4)}-${date.slice(4, 6)}-${date.slice(6, 8)}`;
  const next = document.getElementById(`night-next-${host}`);
  if (next) next.disabled = date >= _lastNight();
  const sum = document.getElementById(`night-sum-${host}`);

  if (!data) {
    grid.innerHTML = '';
    if (sum) sum.textContent = 'Could not load this night from the archive.';
    return;
  }
  if (tw) window.twilightSlider.drawTwilightOnWrap(grid, tw);

  const pct = m => `${Math.max(0, Math.min(100, m / 1440 * 100)).toFixed(2)}%`;
  const nowMin = (Date.now() - d.getTime()) / 60000;
  const now = nowMin >= 0 && nowMin < 1440
    ? `<span class="sa-now" style="left:${pct(nowMin)}" aria-hidden="true"></span>` : '';
  const legendNow = grid.closest('.sa-night')?.querySelector('.sa-legend-now');
  if (legendNow) legendNow.hidden = !now;

  const axis = ['12:00', '15:00', '18:00', '21:00', '00:00', '03:00', '06:00', '09:00', '12:00']
    .map((t, i) => `<span class="sa-mono" style="left:${i * 12.5}%">${t}</span>`).join('');
  const cams = Object.entries(data.cameras || {});
  const notes = [];
  // Older than the night before last: its data should all have arrived.
  const twoBack = new Date(`${_lastNight().replace(/(\d{4})(\d\d)(\d\d)/, '$1-$2-$3')}T12:00:00Z`);
  twoBack.setUTCDate(twoBack.getUTCDate() - 1);
  const old = d < twoBack;
  const stageLabel = { proc: 'Morning processing', up: 'Archive upload' };
  let anyStages = false;
  const lanes = cams.map(([cam, c]) => {
    const st = nightLaneStatus(c, old);
    const spans = c?.spans || [];
    const gaps = nightGaps(spans);
    const worst = gaps.reduce((a, g) => (!a || g[1] - g[0] > a[1] - a[0]) ? g : a, null);
    const camE = escHtml(cam);
    if (worst) notes.push(`${camE} lost ${Math.round(worst[1] - worst[0])} min of colour video at ${_nightHHMM(worst[0])}.`);
    if (!c) notes.push(old ? `${camE} has no station data for this night.` : `${camE} has not uploaded this night yet.`);
    else if (st.tone === 'busy') notes.push(`${camE} is still processing.`);
    const bars = spans.map(([a, b]) => `<span class="sa-bar-cap" style="left:${pct(a)};width:${pct(b - a)}"
        title="${_nightHHMM(a)} to ${_nightHHMM(b)} UTC"></span>`).join('')
      + gaps.map(([a, b]) => `<span class="sa-bar-gap" style="left:${pct(a)};width:${pct(b - a)}"
        title="No colour video ${_nightHHMM(a)} to ${_nightHHMM(b)} UTC"></span>`).join('')
      + nightStageBars(c, now ? nowMin : null).map(b => {
        anyStages = true;
        const when = b.running ? `since ${_nightHHMM(b.from)} UTC, still running` : `${_nightHHMM(b.from)} to ${_nightHHMM(b.to)} UTC`;
        return `<span class="sa-bar-${b.kind}${b.running ? ' running' : ''}" style="left:${pct(b.from)};width:${pct(b.to - b.from)}"
          title="${stageLabel[b.kind]} ${when}"></span>`;
      }).join('');
    return `<span class="sa-mono sa-lane-cam">${camE}</span>
      <div class="sa-lane">${bars}${now}</div>
      <div class="sa-lane-stat">
        <span class="sa-mono sa-lane-n">${c ? c.locked : '—'}</span>
        <span class="sa-stack">
          <span class="sa-tone-${st.tone}">${st.text}</span>
          ${worst ? `<span class="sa-tone-bad">${Math.round(worst[1] - worst[0])} min gap at ${_nightHHMM(worst[0])}</span>` : ''}
        </span>
      </div>
      <span></span>
      <div class="sa-stages">${nightStageCounts(c).map(x => `<span${x.dim ? ` class="dim" title="${escHtml(x.title)}"` : ''}>${x.label}
        <span class="sa-mono">${x.value}</span></span>`).join('')}</div>
      <span></span>`;
  }).join('');
  grid.innerHTML = `<span></span><div class="sa-tl-axis">${axis}</div><span class="sa-note">Meteor clips, status</span>${lanes}`;
  grid.closest('.sa-night')?.querySelectorAll('.sa-legend-stage').forEach(el => { el.hidden = !anyStages; });

  // How far behind the station this copy is: the newest pushed state.json.
  const asOf = document.getElementById(`night-asof-${host}`);
  const at = Math.max(0, ...cams.map(([, c]) => c?.state_at || 0));
  if (asOf) {
    const t = new Date(at * 1000);
    asOf.textContent = !at ? '' : 'Station data from ' + (t.toISOString().slice(0, 10) === new Date().toISOString().slice(0, 10)
      ? '' : t.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' }) + ', ')
      + t.toISOString().slice(11, 16) + ' UTC';
  }

  if (sum) {
    const withData = cams.filter(([, c]) => c && c.total);
    sum.innerHTML = !cams.length ? 'No cameras configured.'
      : !withData.length ? 'No camera has uploaded this night yet.'
      : notes.length ? notes.join(' ')
      : `${cams.length === 1 ? 'The camera' : `All ${cams.length} cameras`} captured the night and finished processing.`;
  }
}

// The station page's minute timer: the night reloads only while Health shows.
function refreshNightIfShown(host) {
  if ((activeSysAdminTab[host] || 'health') === 'health') fetchNight(host);
}

function nightNavDate(host, delta) {
  if (!nightState[host]) nightState[host] = { date: _lastNight() };
  const d = nightState[host].date;
  const dt = new Date(`${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}T12:00:00Z`);
  dt.setUTCDate(dt.getUTCDate() + delta);
  const next = dt.toISOString().slice(0, 10).replace(/-/g, '');
  if (next > _lastNight()) return;
  nightState[host].date = next;
  fetchNight(host);
}

/* ─────────────────────────────────────────
   Updater (read-only status display — updates ship as signed bundles)
───────────────────────────────────────── */
function _setUpdaterUI(host, d) {
  const localEl  = document.getElementById(`upd-local-${host}`);
  const remoteEl = document.getElementById(`upd-remote-${host}`);
  const badge    = document.getElementById(`upd-status-badge-${host}`);
  if (localEl)         localEl.textContent  = d.local  || '—';
  if (remoteEl)        remoteEl.textContent = d.remote || '—';
  if (badge && d.status) {
    badge.textContent = d.status;
    badge.style.color = d.up_to_date ? 'var(--green,#3fb950)' : 'var(--yellow,#d29922)';
  }
  const problem = _updateProblem(d);
  if (badge && problem) {
    badge.textContent = problem;
    badge.style.color = 'var(--red,#f85149)';
  }
}

// The updater's record of its latest attempt (bundle.update, written by
// updater.sh): a text for an update that failed, left services down, or never
// completed; null otherwise or on stations that predate the record.
export function _updateProblem(d) {
  const u = d && d.bundle && d.bundle.update;
  if (!u || typeof u !== 'object') return null;
  const v = u.version || '?';
  if (u.status === 'unhealthy') {
    const svcs = Array.isArray(u.failed_services) ? u.failed_services.join(', ') : '';
    return `update ${v}: not running after restart${svcs ? ': ' + svcs : ''}`;
  }
  if (u.status === 'failed') return `update ${v} failed${u.detail ? ': ' + u.detail : ''}`;
  if (u.status === 'started' && u.version && u.version !== d.local) {
    return `update ${v} not completed (retried at the next updater run)`;
  }
  return null;
}

async function fetchUpdaterStatus(host) {
  let r;
  try {
    r = await window.fetchOnce(`tab:updater:${host}`, `/api/updater/status/${host}`);
  } catch(e) {
    if (window._isAbort(e)) return;
    r = null;
  }
  if (!r || !r.ok) return;
  const d = await r.json().catch(() => null);
  if (!d) return;
  _setUpdaterUI(host, d);
}

// The overlay logo picker: the one station config key the dashboard writes.
// Options are the images in the station's own assets directory.
async function logoRefreshList(host) {
  const sel = document.getElementById(`sa-logo-${host}`);
  if (!sel) return;
  const current = (window.settingsData[host]?.overlay?.logo || '').split('/').pop();
  const r = await fetch(`/api/logo/list/${host}`).catch(() => null);
  const logos = r && r.ok ? await r.json().catch(() => []) : [];
  if (!Array.isArray(logos) || !logos.length) return;
  const names = logos.map(l => l.filename);
  if (current && !names.includes(current)) names.unshift(current);
  sel.innerHTML = names.map(n =>
    `<option value="${escHtml(n)}"${n === current ? ' selected' : ''}>${escHtml(n)}</option>`).join('');
  sel.dataset.paths = JSON.stringify(Object.fromEntries(logos.map(l => [l.filename, l.path])));
  logoPicked(host);
}

function logoPicked(host) {
  const sel = document.getElementById(`sa-logo-${host}`);
  const btn = document.getElementById(`sa-logo-save-${host}`);
  if (!sel || !btn) return;
  const current = (window.settingsData[host]?.overlay?.logo || '').split('/').pop();
  btn.disabled = !sel.value || sel.value === current;
  const msg = document.getElementById(`sa-logo-msg-${host}`);
  if (msg) msg.textContent = '';
}

async function saveOverlayLogo(host, btn) {
  const sel = document.getElementById(`sa-logo-${host}`);
  const msg = document.getElementById(`sa-logo-msg-${host}`);
  if (!sel?.value) return;
  btn.disabled = true;
  const r = await fetch(`/api/overlay/logo/${host}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({filename: sel.value}),
  }).catch(() => null);
  const d = r ? await r.json().catch(() => ({})) : {};
  if (!r || !r.ok || !d.logo) {
    if (msg) msg.textContent = `Not saved: ${d.error || (r ? 'HTTP ' + r.status : 'station unreachable')}`;
    btn.disabled = false;
    return;
  }
  const cfg = window.settingsData[host];
  if (cfg) cfg.overlay = {...(cfg.overlay || {}), logo: d.logo};
  if (msg) msg.textContent = 'Saved. Clips encoded from now on carry it.';
}

async function previewOverlay(host, btn) {
  // The station's own overlay settings, with the logo picked in the
  // dropdown so a choice can be checked before it is saved.
  const ov = window.settingsData[host]?.overlay || {};
  const payload = {
    style: ov.style || 'standard', text_opacity: ov.text_opacity ?? 0.8,
    bar_logo_opacity: ov.bar_logo_opacity ?? 0.9, font_size: ov.font_size ?? 19,
    network: ov.network ?? 'ROVIMEN', coords: ov.coords ?? '',
  };
  if (ov.logo) payload.logo = ov.logo;
  const sel = document.getElementById(`sa-logo-${host}`);
  const picked = sel?.value && JSON.parse(sel.dataset.paths || '{}')[sel.value];
  if (picked) payload.logo = picked;
  for (const k of ['show_logo','show_network','show_station','show_coords','show_pointing','show_timestamp']) {
    payload[k] = ov[k] !== false;
  }
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Generating…';
  try {
    const resp = await fetch(`/api/overlay_preview/${host}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    if (!resp.ok) throw new Error(await resp.text());
    const blob = await resp.blob();
    const imgEl = document.getElementById(`overlay-preview-img-${host}`);
    if (imgEl.src.startsWith('blob:')) URL.revokeObjectURL(imgEl.src);
    imgEl.src = URL.createObjectURL(blob);
    document.getElementById(`overlay-preview-${host}`).hidden = false;
    btn.textContent = prev;
  } catch(e) {
    btn.textContent = 'Error';
    setTimeout(() => { btn.textContent = prev; }, 2000);
  } finally {
    btn.disabled = false;
  }
}

async function refreshSysAdmin(host, btn) {
  btn.disabled = true;
  btn.textContent = '↻ Refreshing…';
  // Force a full rebuild — the user clicked refresh to see fresh structure,
  // not just data updates.
  delete _settingsRenderState[host];
  await Promise.all([window.fetchStatus(host), window.fetchSettings(host)]);
  renderSysAdmin(host);
}

/* ─────────────────────────────────────────
   System Admin — graph drawing
───────────────────────────────────────── */
// A colour token's value, for canvas drawing (canvas can't read var()).
const _tok = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// A sparkline of one vitals field, plus its latest value written as text
// next to it (#vital-<key>-<host>), so the number stays readable and
// selectable instead of being painted into the canvas.
function drawGraph(host, key, history, field, maxVal, color, fmt) {
  const canvas = document.getElementById(`graph-${key}-${host}`);
  const valEl = document.getElementById(`vital-${key}-${host}`);
  const raw = (history || []).map(p => p[field]).filter(v => v !== null && v !== undefined);
  if (valEl) valEl.innerHTML = raw.length ? fmt(Number(raw.at(-1)) || 0) : '—';
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.offsetWidth || canvas.width;
  const H = canvas.height;
  canvas.width = W;
  ctx.clearRect(0, 0, W, H);
  if (raw.length < 2) return;

  const WINDOW = 150; // fixed window size matches ring buffer max (5 min @ 2s)
  const data = raw.slice(-WINDOW);
  const step = W / (WINDOW - 1);
  const x0 = (WINDOW - data.length) * step;  // data fills the right of the window
  const yOf = v => H - 2 - Math.max(0, Math.min(v, maxVal)) / maxVal * (H - 4);
  ctx.beginPath();
  data.forEach((v, i) => i ? ctx.lineTo(x0 + i * step, yOf(v)) : ctx.moveTo(x0, yOf(v)));
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.stroke();
}

function drawAllGraphs(host) {
  const h = window.vitalsHistory[host] || [];
  // Dynamic ceiling for disk write: at least 10 MB/s, or 1.5× the observed max
  const maxWrite = Math.max(10, ...h.map(p => p.disk_write_mbps ?? 0)) * 1.5;
  const unit = u => `<span class="sa-unit">${u}</span>`;
  drawGraph(host, 'cpu',  h, 'cpu_pct',         100,      _tok('--hdr-teal'), v => v.toFixed(0) + unit('%'));
  drawGraph(host, 'temp', h, 'temp_c',            90,      _tok('--orange'),   v => v.toFixed(0) + unit('°C'));
  drawGraph(host, 'ram',  h, 'ram_pct',          100,      _tok('--blue'),     v => v.toFixed(0) + unit('%'));
  drawGraph(host, 'disk', h, 'disk_write_mbps', maxWrite,  _tok('--purple'),   v => v.toFixed(v < 10 ? 1 : 0) + unit('MB/s'));
  renderCoreGraph(host);
  renderTopProcs(host);
}

// One labelled bar: load share coloured by how busy it is.
const _loadColor = f => f > 0.8 ? 'var(--red)' : f > 0.5 ? 'var(--yellow)' : 'var(--hdr-teal)';
const _barRow = (label, frac, value, title = '') => `<div class="sa-bar-row"${title ? ` title="${escHtml(title)}"` : ''}>
    <span class="sa-mono">${label}</span>
    <div class="sa-track"><div style="width:${Math.min(frac * 100, 100).toFixed(1)}%;background:${_loadColor(frac)}"></div></div>
    <span class="sa-mono">${value}</span>
  </div>`;

function renderCoreGraph(host) {
  const el = document.getElementById(`graph-cores-${host}`);
  if (!el) return;
  const cores = window.coreData[host]?.cores;
  if (!cores?.length) {
    el.innerHTML = '<span class="sa-note">Waiting for data…</span>';
    return;
  }
  el.innerHTML = cores.map((c, i) => {
    const pct = c.load_pct ?? 0;
    const ghz = c.freq_ghz != null ? ` <span class="sa-unit">${c.freq_ghz.toFixed(1)} GHz</span>` : '';
    return _barRow(`cpu${i}`, pct / 100, `${pct}%${ghz}`);
  }).join('');
}

function renderTopProcs(host) {
  const el = document.getElementById(`graph-top-procs-${host}`);
  if (!el) return;
  const data = window.coreData[host];
  const procs = data?.top_procs;
  const total = data?.total_cores;
  if (!procs?.length) return;
  el.innerHTML = procs.map(p => {
    const cores = Number(p.cores) || 0;
    return _barRow(escHtml(p.name), total ? cores / total : 0,
      `${cores.toFixed(1)} <span class="sa-unit">cores</span>`, p.name);
  }).join('');
}

/* ─────────────────────────────────────────
   System Admin — cron jobs
───────────────────────────────────────── */
async function fetchCrons(host) {
  try {
    const r = await window.fetchOnce(`tab:crons:${host}`, `/api/crons/${host}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    window.cronsData[host] = await r.json();
  } catch(e) {
    if (window._isAbort?.(e)) return;
    window.cronsData[host] = [];
  }
  renderCronsTable(host);
}

export function _parseCronSchedule(sched) {
  const s = sched.trim();
  if (s === '@reboot')              return { label: 'At reboot',    detail: '' };
  if (s === '@hourly')              return { label: 'Every hour',   detail: 'at :00' };
  if (s === '@daily' || s === '@midnight') return { label: 'Daily', detail: 'at midnight UTC' };
  if (s === '@weekly')              return { label: 'Weekly',       detail: 'Sunday midnight' };
  if (s === '@monthly')             return { label: 'Monthly',      detail: '1st at midnight' };

  const parts = s.split(/\s+/);
  if (parts.length !== 5) return { label: s, detail: '' };
  const [min, hour, dom, month, dow] = parts;

  const dayNames = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const isNum = v => /^\d+$/.test(v);

  // Every N minutes, optionally restricted to a hour range: */N <*|range> * * *
  if (min.startsWith('*/') && dom === '*' && month === '*' && dow === '*') {
    const n = min.slice(2);
    if (hour === '*') return { label: `Every ${n} min`, detail: '' };
    // Hour range like 4-10
    const hm = hour.match(/^(\d+)-(\d+)$/);
    if (hm) return { label: `Every ${n} min`, detail: `${hm[1].padStart(2,'0')}:00–${hm[2].padStart(2,'0')}:59 UTC` };
    return { label: `Every ${n} min`, detail: `hour ${hour} UTC` };
  }
  // Stepped minute range: M-N/S * * * *  (e.g. 5-55/10)
  if (/^\d+-\d+\/\d+$/.test(min) && hour === '*' && dom === '*' && month === '*' && dow === '*') {
    const step = min.split('/')[1];
    return { label: `Every ${step} min`, detail: '' };
  }
  // Every N hours: M */N * * *
  if (hour.startsWith('*/') && dom === '*' && month === '*' && dow === '*') {
    const n = hour.slice(2);
    return { label: `Every ${n}h`, detail: isNum(min) ? `at :${min.padStart(2,'0')}` : '' };
  }
  // Every minute: * * * * *
  if (min === '*' && hour === '*' && dom === '*' && month === '*' && dow === '*') {
    return { label: 'Every minute', detail: '' };
  }
  // Hourly at :MM —  M * * * *  (P1-44, very common idiom that used to fall
  // through to raw display).
  if (isNum(min) && hour === '*' && dom === '*' && month === '*' && dow === '*') {
    return { label: 'Hourly', detail: `at :${min.padStart(2,'0')}` };
  }
  // Weekly on a specific day: M H * * D
  if (isNum(min) && isNum(hour) && dom === '*' && month === '*' && isNum(dow)) {
    const h = hour.padStart(2,'0'), m = min.padStart(2,'0');
    const d = dayNames[parseInt(dow)] || dow;
    return { label: `Weekly (${d})`, detail: `at ${h}:${m} UTC` };
  }
  // Daily at fixed time: M H * * *
  if (isNum(min) && isNum(hour) && dom === '*' && month === '*' && dow === '*') {
    const h = hour.padStart(2,'0'), m = min.padStart(2,'0');
    return { label: 'Daily', detail: `at ${h}:${m} UTC` };
  }
  // Fallback: show raw schedule in a slightly abbreviated form
  return { label: s, detail: '' };
}

function renderCronsTable(host) {
  const el = document.getElementById(`crons-table-${host}`);
  if (!el) return;
  const crons = window.cronsData[host] || [];
  if (!crons.length) {
    el.innerHTML = '<span class="sa-note">No scheduled jobs found</span>';
    return;
  }
  el.innerHTML = `<ul class="sa-list">${crons.map(c => {
    const { label, detail } = _parseCronSchedule(c.schedule);
    const src = c.source === 'crontab' ? 'user crontab' : c.source.replace('/etc/cron.d/', 'cron.d/');
    return `<li class="sa-cron">
      <span>${escHtml(label)}${detail ? ` <span class="sa-note">${escHtml(detail)}</span>` : ''}</span>
      <span class="sa-mono sa-cron-cmd" title="${escHtml(c.command)}">${escHtml(c.command)}</span>
      <span class="sa-note">${escHtml(src)}</span>
    </li>`;
  }).join('')}</ul>`;
}

/* ─────────────────────────────────────────
   System Admin — janitor / storagewatch
───────────────────────────────────────── */
async function fetchStoragewatchStatus(host) {
  try {
    const r = await window.fetchOnce(`tab:storagewatch:${host}`, `/api/storagewatch/${host}`);
    if (!r.ok) { window.storagewatchData[host] = null; renderJanitorSection(host); return; }
    window.storagewatchData[host] = await r.json();
  } catch(e) {
    if (window._isAbort?.(e)) return;
    window.storagewatchData[host] = null;
  }
  renderJanitorSection(host);
}

function renderJanitorSection(host) {
  const el = document.getElementById(`janitor-section-${host}`);
  if (!el) return;
  const d = window.storagewatchData[host];
  if (!d || d.error) {
    el.innerHTML = `<span class="sa-note">Janitor: ${d?.error === 'no_state' ? 'no data yet (runs every 10 min)' : 'unavailable'}</span>`;
    return;
  }
  const relTime = t => {
    const s = Math.floor((Date.now() - new Date(t)) / 1000);
    if (s < 60) return `${s} s ago`;
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    return `${Math.floor(s / 3600)} h ago`;
  };
  el.innerHTML = `<dl class="sa-kv">
    <dt>Janitor last ran</dt><dd class="sa-mono">${relTime(d.last_run)}</dd>
    <dt>Freed that run</dt><dd class="sa-mono">${d.deleted_gb > 0 ? d.deleted_gb + ' GB' : 'Nothing'}</dd>
  </dl>`;
}
/* ─────────────────────────────────────────
   System Admin — recent station events
   (/api/station_events/<host>: boots, uplink changes, offline/online)
───────────────────────────────────────── */
const _EVENT_LABELS = {
  boot: 'Boot', uplink: 'Uplink', rescue_wifi: 'Rescue Wi-Fi',
  offline: 'Offline', online: 'Online', station_log: 'Station log',
};

// One table row per event: time UTC, event, detail. Pure, for the tests.
export function _stationEventsHtml(events) {
  if (!Array.isArray(events) || !events.length) {
    return '<span class="sa-note">No events recorded yet.</span>';
  }
  const rows = events.map(ev => {
    const when = String(ev.ts || '').replace('T', ' ').replace(/Z$/, '');
    const label = _EVENT_LABELS[ev.kind] || ev.kind || '?';
    const d = ev.detail;
    let text = '';
    if (d && typeof d === 'object' && !Array.isArray(d)) text = d.text || JSON.stringify(d);
    else if (d != null) text = String(d);
    // Who says so: the station about itself, or the VPS from what it observed.
    const source = ev.source === 'vps' ? 'VPS' : ev.source === 'station' ? 'Station' : (ev.source || '?');
    // A station time the VPS could not accept (future, or past retention) is
    // shown as the receipt time; say so, with the time the station claimed.
    const recv = String(ev.received_at || '').replace('T', ' ').replace(/Z$/, '');
    const tip = ev.ts_clamped
      ? `Station sent ${ev.sent_ts || '?'}; shown as received ${recv}`
      : `Received ${recv}`;
    return `<tr><td class="sa-mono" title="${escHtml(tip)}">${escHtml(when)}${ev.ts_clamped ? ' *' : ''}</td><td>${escHtml(label)}</td><td class="sa-note">${escHtml(source)}</td><td>${escHtml(text)}</td></tr>`;
  }).join('');
  return `<table class="sa-events"><thead><tr><th scope="col">Time (UTC)</th><th scope="col">Event</th><th scope="col">Source</th><th scope="col">Detail</th></tr></thead><tbody>${rows}</tbody></table>`;
}

async function fetchStationEvents(host) {
  const card = document.getElementById(`events-card-${host}`);
  const el = document.getElementById(`events-list-${host}`);
  if (!card || !el) return;
  try {
    const r = await window.fetchOnce(`tab:station_events:${host}`, `/api/station_events/${host}?limit=20`);
    if (r.status === 403 || r.status === 401) { card.hidden = true; return; }
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    card.hidden = false;
    el.innerHTML = _stationEventsHtml(data.events);
  } catch (e) {
    if (window._isAbort?.(e)) return;
    card.hidden = false;
    el.innerHTML = '<span class="sa-note">Events unavailable</span>';
  }
}

/* ─────────────────────────────────────────
   Log Viewer
───────────────────────────────────────── */
const LOGS_SERVICES = [
  'color-capture',
  'rovimen-station-api',
  'dawn',
  'stacker',
  'encoder',
  'timelapse',
  'janitor',
];

window.logsState = { service: LOGS_SERVICES[0], lines: 200, minLevel: 'info', filter: '', live: false, timer: null };

const _LOG_LEVEL_BTNS = [['debug', 'All'], ['info', 'Info and up'], ['warning', 'Warnings'], ['error', 'Errors']];

function renderLogsShell() {
  const host = state.activeStation;
  const el = document.getElementById(`logs-section-${host}`);
  if (!el) return;
  // Rebuilding the toolbar replaces the Follow button with a fresh one, off:
  // stop any running interval so it doesn't leak across station switches.
  clearInterval(window.logsState.timer);
  window.logsState.timer = null;
  window.logsState.live = false;
  // One RMS log per camera, from the station's config
  const cams = Object.keys((window.settingsData[host] || {}).stations || {});
  const allSources = [...LOGS_SERVICES, ...cams.map(c => 'rms-' + c)];
  // Ensure selected service is still valid after station switch
  if (!allSources.includes(window.logsState.service)) window.logsState.service = allSources[0];
  const { service, lines, minLevel } = window.logsState;
  const src = s => `<button type="button" class="sa-src sa-mono" data-src="${escHtml(s)}"
      aria-current="${s === service}" onclick="logsSelectService(this.dataset.src)">${escHtml(s.startsWith('rms-') ? s.slice(4) : s)}</button>`;
  const label = s => s.startsWith('rms-') ? `RMS ${s.slice(4)}` : s;
  el.innerHTML = `<div class="sa-logs">
    <nav class="sa-index" aria-label="Log source">
      <span class="sa-note">Pipeline</span>${LOGS_SERVICES.map(src).join('')}
      ${cams.length ? `<span class="sa-note">RMS</span>${cams.map(c => src('rms-' + c)).join('')}` : ''}
    </nav>
    <section class="sa-card sa-log-card" aria-labelledby="logs-h">
      <div class="sa-log-head">
        <h2 id="logs-h" class="sa-mono">${escHtml(label(service))}</h2>
        <div class="sa-log-tools">
          <div class="sa-seg" role="group" aria-label="Show levels">
            ${_LOG_LEVEL_BTNS.map(([lv, txt]) => `<button type="button" data-lv="${lv}" aria-pressed="${lv === minLevel}"
                onclick="logsOnLevelChange('${lv}')">${txt}<span class="sa-mono sa-count" id="logs-n-${lv}"></span></button>`).join('')}
          </div>
          <input id="logs-filter" type="search" class="sa-field" placeholder="Filter lines" aria-label="Filter lines"
                 autocomplete="off" spellcheck="false" oninput="logsOnFilterChange(this.value)">
          <select id="logs-lines" class="sa-field" aria-label="How many lines" onchange="logsOnLinesChange(this.value)">
            ${[[100, 'Last 100 lines'], [200, 'Last 200 lines'], [500, 'Last 500 lines'], [0, 'Whole log']]
              .map(([n, t]) => `<option value="${n}"${n === lines ? ' selected' : ''}>${t}</option>`).join('')}
          </select>
          <button type="button" class="sa-btn sa-follow" id="logs-live-toggle" aria-pressed="false"
                  onclick="logsToggleLive(this.getAttribute('aria-pressed') !== 'true')"><i aria-hidden="true"></i>Follow</button>
        </div>
      </div>
      <div id="logs-output" class="sa-log" role="log" aria-label="${escHtml(label(service))} log">Loading…</div>
      <div class="sa-log-foot"><span id="logs-shown"></span><span id="logs-ts"></span></div>
    </section>
  </div>`;
}

function logsSelectService(svc) {
  window.logsState.service = svc;
  renderLogsShell();
  fetchLogs();
}

function logsOnLinesChange(val) {
  window.logsState.lines = parseInt(val, 10);
  fetchLogs();
}

function logsOnLevelChange(val) {
  window.logsState.minLevel = val;
  document.querySelectorAll('.sa-log-card [data-lv]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.lv === val)));
  logsApplyFilter();
}

function logsOnFilterChange(val) {
  window.logsState.filter = val.toLowerCase();
  logsApplyFilter();
}

function logsToggleLive(on) {
  window.logsState.live = on;
  document.getElementById('logs-live-toggle')?.setAttribute('aria-pressed', String(on));
  clearInterval(window.logsState.timer);
  window.logsState.timer = null;
  if (on) {
    fetchLogs();
    window.logsState.timer = setInterval(fetchLogs, 5000);
  }
}

let _logsRawLines = [];

async function fetchLogs() {
  if (state.activeTab !== 'settings') return;
  const host = state.activeStation;
  const { service, lines } = window.logsState;
  try {
    const resp = await window.fetchOnce(`tab:logs:${host}:${service}`, `/api/logs/${host}/${service}?lines=${lines}`);
    const data = await resp.json();
    const out = document.getElementById('logs-output');
    const ts  = document.getElementById('logs-ts');
    if (!out) return;
    if (data.error) { out.textContent = 'Error: ' + data.error; return; }
    _logsRawLines = data.lines;
    logsApplyFilter();
    if (ts) ts.textContent = (window.logsState.live ? 'Updates every 5 s, last ' : 'Loaded ')
      + new Date().toLocaleTimeString('en-GB', {timeZone: 'UTC'}) + ' UTC';
  } catch(e) {
    if (window._isAbort?.(e)) return;
    const out = document.getElementById('logs-output');
    if (out) out.textContent = 'Failed to load logs: ' + e.message;
  }
}

const _LOG_LEVELS = { debug: 0, info: 1, warning: 2, error: 3 };

export function _logLineLevel(line) {
  if (/\bERROR\b|\bCRITICAL\b/i.test(line)) return 'error';
  if (/\bWARNING\b|\bWARN\b/i.test(line))   return 'warning';
  if (/\bDEBUG\b/i.test(line))               return 'debug';
  return 'info';
}

// A line's leading timestamp as HH:MM:SS, and the rest. Handles Python
// logging ("2026-10-22 05:19:41,123 ...") and journald ("Oct 22 05:19:41
// host unit[pid]: ..."), minus a leading level word; anything else keeps
// the whole line as the message.
export function _splitLogLine(line) {
  let m = /^\[?\d{4}-\d{2}-\d{2}[ T](\d{2}:\d{2}:\d{2})(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\]?\s*(.*)$/.exec(line);
  if (!m) m = /^[A-Z][a-z]{2} +\d{1,2} (\d{2}:\d{2}:\d{2}) \S+ [^:\s]+: ?(.*)$/.exec(line);
  if (!m) return { time: '', msg: line };
  // The level column shows the level, so drop it from the message's head.
  return { time: m[1], msg: m[2].replace(/^(?:-\s*)?(?:DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\b[\s:-]*/, '') };
}

function logsApplyFilter() {
  const out = document.getElementById('logs-output');
  if (!out) return;
  const needle  = window.logsState.filter;
  const minRank = _LOG_LEVELS[window.logsState.minLevel] ?? 0;
  const levels  = _logsRawLines.map(_logLineLevel);
  for (const lv of ['warning', 'error']) {
    const n = levels.filter(l => l === lv).length;
    const el = document.getElementById(`logs-n-${lv}`);
    if (el) el.textContent = n ? ` ${n}` : '';
  }
  const rows = [];
  _logsRawLines.forEach((line, i) => {
    const lvl = levels[i];
    if ((_LOG_LEVELS[lvl] ?? 1) < minRank) return;
    if (needle && !line.toLowerCase().includes(needle)) return;
    const { time, msg } = _splitLogLine(line);
    rows.push(`<div class="sa-log-row lv-${lvl}"><span>${escHtml(time)}</span><span>${lvl === 'warning' ? 'WARN' : lvl.toUpperCase()}</span><span>${escHtml(msg)}</span></div>`);
  });
  out.innerHTML = rows.join('') || '<span class="sa-note">No log lines match.</span>';
  out.scrollTop = out.scrollHeight;
  const shown = document.getElementById('logs-shown');
  if (shown) shown.textContent = `Showing ${rows.length} of ${_logsRawLines.length} lines`;
}

// Expose to global scope for inline onclick handlers
window.fetchLogs = fetchLogs;
window.logsOnFilterChange = logsOnFilterChange;
window.logsToggleLive = logsToggleLive;
window.drawAllGraphs = drawAllGraphs;
window.fetchCrons = fetchCrons;
window.fetchStoragewatchStatus = fetchStoragewatchStatus;
window.fetchStationEvents = fetchStationEvents;
window.renderLogsShell = renderLogsShell;
window.logsSelectService = logsSelectService;
window.logsOnLevelChange = logsOnLevelChange;
window.logsOnLinesChange = logsOnLinesChange;
window.renderSysAdmin = renderSysAdmin;
window.renderSysAdminIfChanged = renderSysAdminIfChanged;
window.refreshNightIfShown = refreshNightIfShown;
window.switchSysAdminTab = switchSysAdminTab;
window.refreshSysAdmin = refreshSysAdmin;
window.nightNavDate = nightNavDate;
window._lastNight = _lastNight;
window.saveDataVisibility = saveDataVisibility;
window.logoPicked = logoPicked;
window.saveOverlayLogo = saveOverlayLogo;
window.previewOverlay = previewOverlay;
