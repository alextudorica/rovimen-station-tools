/*
 * dashboard-common.js
 * ───────────────────
 * Shared utilities and state used across all dashboard pages. Loaded
 * first by every page; declares cross-page globals on `window`, page-
 * agnostic formatters, the logo/app-panel dropdown, and the UTC clock.
 *
 * Keep this file lean — it ships on every dashboard navigation.
 */

/* global closeCamModal, stackCloseModal, arcModalClose, ovPlotClose,
          vdbCloseModal, vdbCloseSyncedModal, closeLiveModal,
          closeModal */

/* ─────────────────────────────────────────
   Utility: clamp all <input type="date"> on the page to today (UTC).
   Captured nights can't be in the future. Call from each page init.
───────────────────────────────────────── */
export function clampDateInputsToToday(){
  const today = new Date().toISOString().slice(0,10); // UTC YYYY-MM-DD
  document.querySelectorAll('input[type="date"]').forEach(el => { el.max = today; });
}

/* ─────────────────────────────────────────
   State — populated from /api/stations
───────────────────────────────────────── */
export const state = {
  STATIONS_META: {},
  VDB_CAMERAS: {},
  IS_ADMIN: false,
  AUTH_USER: null,
  // May hide detections as false positives (/api/auth/status can_hide).
  CAN_HIDE: false,
  USER_ROLE: 'guest',
  USER_STATIONS: [],
  activeStation: null,
  // Overview map multi-select: set of station host keys currently selected.
  // activeStation tracks the most-recently-toggled host so legacy
  // single-station controls still resolve a host.
  selectedStations: new Set(),
  activeTab: 'rms',
  statusData: {},
  vitalsData: {},
  tlData: {},
  // What the user is looking at: camera, night (YYYYMMDD) and UTC time
  // window (HH:MM). A value is set only when the user picks it (or a link
  // carries it), and every tab reads it, so switching camera, night, tab or
  // station changes only the thing that was switched. null = not pinned: the
  // tab uses its own default (first camera, newest night, twilight window).
  view: { cam: null, date: null, from: null, to: null },
};

/* ─────────────────────────────────────────
   Why a detection has no playable clip
   pending: the clip has not reached the archive yet (the server's shared
            upload state, dashboard/upload_state.py: "not uploaded yet",
            or "upload overdue" once the night's dawn upload should have
            finished).
   no_clip: the camera records no video at all.
   pruned:  the archive's 14-day retention removed the video; stack and data
            are kept.
───────────────────────────────────────── */
export function clipStatus(d) {
  if (d && d.pruned) {
    return { label: 'removed', badge: 'REMOVED',
      title: 'Detected — the video was removed from the archive after 14 days; the stack and data are kept' };
  }
  if (d && d.no_clip) {
    return { label: 'no video', badge: 'NO VIDEO', title: 'Detected — this camera does not record video' };
  }
  if (d && d.overdue) {
    return { label: 'upload overdue', badge: 'OVERDUE',
      title: 'Detected — the clip should have been uploaded after dawn processing but is not in the archive yet' };
  }
  return { label: 'not uploaded yet', badge: 'NOT UPLOADED',
    title: 'Detected — the clip is not uploaded yet; stations upload clips after dawn processing' };
}

/* ─────────────────────────────────────────
   Admin-only nav links (templates/_nav.html)
   Calibration, Social and Admin render hidden except on their own page.
   initNavAuth calls this once the page's /api/auth/status lands, after
   fetchAuthStatus has written state.AUTH_USER, so the links also mark "auth
   applied" (the E2E specs wait on #nav-tools for that).
───────────────────────────────────────── */
const ADMIN_NAV_IDS = ['nav-tools', 'logo-dd-calibration', 'logo-dd-social', 'logo-dd-admin'];
export function setAdminNav(isAdmin) {
  for (const id of ADMIN_NAV_IDS) {
    const el = document.getElementById(id);
    if (el && !el.classList.contains('active')) el.style.display = isAdmin ? '' : 'none';
  }
  document.querySelectorAll('.nav-panel-tools').forEach((el) => { el.hidden = !isAdmin; });
}

/* ─────────────────────────────────────────
   View state in the URL
   ?cam=RO000W&date=20260920&from=15:32&to=15:40 mirrors state.view (plus
   page-specific keys such as tab=) with replaceState, so reload, Back and a
   pasted link land on the same camera, night and time window.
───────────────────────────────────────── */
const VIEW_CAM_RE  = /^[A-Za-z0-9_-]{2,16}$/;
const VIEW_DATE_RE = /^\d{8}$/;
const VIEW_HHMM_RE = /^([01]\d|2[0-3]):[0-5]\d$/;
export const VIEW_KEYS = ['cam', 'date', 'from', 'to'];

/**
 * fetch() that waits out "not yet" answers and retries: 429 (rate limit,
 * Retry-After default 15 s), 202 (a clip the server is still pulling from
 * the station) and 503 (every remux slot busy), both default 3 s. A bulk
 * download paces itself to a per-minute limit instead of silently losing
 * requests, and a station-only clip arrives instead of its placeholder.
 * `onWait(seconds)` reports each wait. Returns the last response (still
 * waiting after `tries` attempts).
 */
export async function fetchPaced(url, onWait = () => {}, { tries = 4, sleep, init } = {}) {
  const pause = sleep || (ms => new Promise(r => setTimeout(r, ms)));
  let resp = null;
  for (let i = 0; i < tries; i++) {
    resp = init ? await fetch(url, init) : await fetch(url);
    if (resp.status !== 429 && resp.status !== 202 && resp.status !== 503) return resp;
    if (i + 1 >= tries) break;
    const dflt = resp.status === 429 ? 15 : 3;
    const s = Math.min(60, Math.max(1, parseInt(resp.headers.get('Retry-After') || String(dflt), 10) || dflt));
    onWait(s);
    await pause(s * 1000);
  }
  return resp;
}

/** A detection's UTC time cut to the second ("2026-10-21T19:00:22"), the key
 *  that pairs a clip's meteor_time with its RMS detection. time_utc carries
 *  milliseconds since #1397 and meteor_time always did, so both sides must be
 *  cut; comparing either one whole matches nothing. */
export function utcSecondKey(t) {
  return t ? String(t).slice(0, 19) : null;
}

/** Same clip under either name: a clip and its ``.details`` overlay render
 *  (``X_color.mkv`` / ``X_color.details.mkv``) are one clip to the viewer. */
export function sameClip(a, b) {
  const base = n => String(n || '').replace(/\.details(\.(mkv|mp4))$/, '$1');
  return !!a && !!b && base(a) === base(b);
}

/** Parse cam/date/from/to out of a query string. Invalid values come back
 *  as null, and from/to only as a pair, so a hand-edited URL can't put a
 *  half-open window into the filters. `extraKeys` are passed through raw. */
export function parseViewParams(search, extraKeys = []) {
  const p = new URLSearchParams(search || '');
  const cam = p.get('cam');
  const date = p.get('date');
  const from = p.get('from');
  const to = p.get('to');
  const out = {
    cam:  cam && VIEW_CAM_RE.test(cam) ? cam : null,
    date: date && VIEW_DATE_RE.test(date) ? date : null,
    from: null,
    to:   null,
  };
  if (from && to && VIEW_HHMM_RE.test(from) && VIEW_HHMM_RE.test(to)) {
    out.from = from;
    out.to = to;
  }
  for (const k of extraKeys) out[k] = p.get(k);
  return out;
}

/** Query string for `view` over `keys` in that order ('' when nothing is
 *  set). Colons stay literal so the time window reads as 15:32 in the bar. */
export function viewQuery(view, keys = VIEW_KEYS) {
  const p = new URLSearchParams();
  for (const k of keys) {
    const v = view ? view[k] : null;
    if (v != null && v !== '') p.set(k, String(v));
  }
  const s = p.toString().replace(/%3A/gi, ':');
  return s ? `?${s}` : '';
}

function _ymdToUtcMs(ymd) {
  return Date.UTC(+ymd.slice(0, 4), +ymd.slice(4, 6) - 1, +ymd.slice(6, 8));
}

/** The night in `nights` (YYYYMMDD strings) closest to `wanted`: `wanted`
 *  itself when present, else the nearest by date (the older one on a tie).
 *  null when `wanted` is unset or there are no nights, so callers fall back
 *  to their own default. */
export function nearestNight(nights, wanted) {
  if (!wanted || !VIEW_DATE_RE.test(wanted) || !nights || !nights.length) return null;
  if (nights.includes(wanted)) return wanted;
  const target = _ymdToUtcMs(wanted);
  let best = null;
  let bestDist = Infinity;
  for (const n of nights) {
    if (!VIEW_DATE_RE.test(n)) continue;
    const dist = Math.abs(_ymdToUtcMs(n) - target);
    if (dist < bestDist || (dist === bestDist && n < best)) {
      best = n;
      bestDist = dist;
    }
  }
  return best;
}

// URL syncs with a write still scheduled; _modalOpen flushes them.
const _pendingUrlSyncs = new Set();

/** Debounced replaceState of the current path + `query()`. Slider drags fire
 *  input events continuously and browsers throttle history calls (Safari
 *  throws after 100 in 30 s), so writes are coalesced. Returns the scheduler;
 *  call `.flush()` to write immediately (e.g. before a pushState). */
export function createUrlSync(query, delayMs = 250) {
  let timer = null;
  const write = () => {
    timer = null;
    _pendingUrlSyncs.delete(schedule);
    const url = window.location.pathname + query();
    if (url !== window.location.pathname + window.location.search) {
      history.replaceState(history.state, '', url);
    }
  };
  const schedule = () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(write, delayMs);
    _pendingUrlSyncs.add(schedule);
  };
  schedule.flush = () => {
    if (timer) clearTimeout(timer);
    write();
  };
  return schedule;
}

/* ─────────────────────────────────────────
   Meteor shower IAU code → full name
   Source: IAU Meteor Data Center working list
───────────────────────────────────────── */
export const SHOWER_NAMES = {
  // ── Sporadic ────────────────────────────
  SPO: 'Sporadic',
  ANT: 'Antihelion Source',

  // ── January ─────────────────────────────
  QUA: 'Quadrantids',

  // ── February ────────────────────────────
  ACE: 'alpha Centaurids',

  // ── March ───────────────────────────────
  GNO: 'gamma Normids',

  // ── April ───────────────────────────────
  LYR: 'April Lyrids',
  ELY: 'eta Lyrids',

  // ── May ─────────────────────────────────
  ETA: 'eta Aquariids',
  XHE: 'chi Herculids',

  // ── June (mostly daytime) ───────────────
  ARI: 'Daytime Arietids',
  ZPE: 'Daytime zeta Perseids',
  BTA: 'Daytime beta Taurids',
  JBO: 'June Bootids',
  JPE: 'July Pegasids',

  // ── July ────────────────────────────────
  JXA: 'July xi Arietids',
  PAU: 'Piscis Austrinids',
  CAP: 'alpha Capricornids',
  SDA: 'Southern delta Aquariids',
  NDA: 'Northern delta Aquariids',

  // ── August ──────────────────────────────
  PER: 'Perseids',
  KCG: 'kappa Cygnids',
  MIC: 'Microscopiids',

  // ── September ───────────────────────────
  AUR: 'Aurigids',
  SPE: 'September epsilon Perseids',
  DSX: 'Daytime Sextantids',
  SSG: 'Southern mu Sagittariids',

  // ── October ─────────────────────────────
  DRA: 'October Draconids',
  ORI: 'Orionids',
  EGE: 'epsilon Geminids',
  STA: 'Southern Taurids',
  NTA: 'Northern Taurids',
  TAU: 'Taurids',
  LMI: 'Leonis Minorids',

  // ── November ────────────────────────────
  LEO: 'Leonids',
  AMO: 'alpha Monocerotids',
  AND: 'Andromedids',
  NOO: 'November Orionids',
  NPI: 'Northern delta Piscids',

  // ── December ────────────────────────────
  MON: 'December Monocerotids',
  HYD: 'sigma Hydrids',
  GEM: 'Geminids',
  COM: 'Comae Berenicids',
  URS: 'Ursids',
  PHO: 'Phoenicids',
  ICE: 'iota Cassiopeiids',
};

export function showerFullName(code) {
  if (!code) return 'Sporadic';
  return SHOWER_NAMES[code.toUpperCase()] || code;
}

/* ─────────────────────────────────────────
   Formatters
───────────────────────────────────────── */
export function fmtBytes(b) {
  if (b == null) return '—';
  const u = ['B','KB','MB','GB','TB'];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(1) + '\u00a0' + u[i];
}
export function fmtDate(s) {
  if (!s) return '—';
  return s.slice(0,4) + '-' + s.slice(4,6) + '-' + s.slice(6,8);
}
export function parseFn(fn) {
  const m = fn.match(/_(\d{8})_(\d{6})/);
  if (!m) return '';
  return fmtDate(m[1]) + ' ' + m[2].slice(0,2)+':'+m[2].slice(2,4)+':'+m[2].slice(4,6)+' UTC';
}
export function barColor(pct) {
  return pct > 85 ? 'var(--red)' : pct > 70 ? 'var(--yellow)' : 'var(--green)';
}

/* ─────────────────────────────────────────
   App panel (About, etc.)
───────────────────────────────────────── */
const _APP_PANELS = {
  about: {
    title: 'About ROVIMEN',
    html: `<div class="app-panel-body-inner ap-about">
      <div class="ap-intro">
        <img class="ap-logo" src="/static/img/rovimen_logo.svg" alt="ROVIMEN, Romanian Video Meteor Network" width="220" height="103" decoding="async">
        <p class="ap-lede"><strong>ROVIMEN</strong>, the <em class="ap-acronym">RO</em>manian <em class="ap-acronym">VI</em>deo <em class="ap-acronym">ME</em>teor <em class="ap-acronym">NE</em>twork, started in 2013 as the first scientific video meteor camera network over Romania and has since grown into a node of the <a href="https://globalmeteornetwork.org" target="_blank" rel="noopener">Global Meteor Network</a> (GMN). Its automated video cameras capture and analyse meteors every clear night at member sites in Romania, Moldova and Germany.</p>
      </div>

      <section class="ap-sec">
        <h3>Who runs it</h3>
        <ul class="ap-people">
          <li><strong>Dr. Alexandru Tudorică</strong><span>Coordinator</span><a href="mailto:maintainer-1@example.org">maintainer-1@example.org</a></li>
          <li><strong>Florin Dumitrescu</strong><span>Software, operations, design</span><a href="mailto:maintainer-2@example.org">maintainer-2@example.org</a></li>
        </ul>
      </section>

      <section class="ap-sec">
        <h3>This website</h3>
        <p>Every ROVIMEN station in one place: live camera feeds, each night's RMS detections per camera, meteors matched across stations for triangulated trajectories, a searchable video database with colour-calibrated clips, a long-term archive of stacks and data products, and station health monitoring.</p>
      </section>

      <section class="ap-sec">
        <h3>Global Meteor Network</h3>
        <p>The <strong>Global Meteor Network</strong> is a science project building a worldwide network of low-cost video meteor cameras that watch the night sky continuously. Data from hundreds of stations is used to compute precise meteor trajectories, identify meteoroid streams and study the composition of small solar system bodies. ROVIMEN is its Romanian node.</p>
        <p class="ap-links">
          <a href="https://globalmeteornetwork.org" target="_blank" rel="noopener">globalmeteornetwork.org</a>
          <a href="https://github.com/CroatianMeteorNetwork/RMS" target="_blank" rel="noopener">RMS, the station software (GitHub)</a>
        </p>
      </section>

      <section class="ap-sec">
        <h3>Partners</h3>
        <p>ROVIMEN works in collaboration with the <a href="https://astromania.org" target="_blank" rel="noopener">Astromania Association</a>.</p>
        <div class="ap-partners">
          <a href="https://globalmeteornetwork.org" target="_blank" rel="noopener" title="Global Meteor Network">
            <img src="/static/img/gmn_sphere.png" alt="Global Meteor Network" width="44" height="44" decoding="async" loading="lazy" class="ap-logo-gmn">
          </a>
          <a href="https://astromania.org" target="_blank" rel="noopener" title="Astromania">
            <img src="/static/img/astromania.png" alt="Astromania" width="137" height="14" decoding="async" loading="lazy" class="ap-logo-astro">
          </a>
        </div>
        <h4 class="ap-sub">Participating associations</h4>
        <ul class="ap-orgs">
          <li><a href="https://www.astroclubul.ro/" target="_blank" rel="noopener"><span class="ap-org-logo"><img src="/static/img/astroclubul_bucuresti.png" alt="" width="158" height="22" decoding="async" loading="lazy"></span>Astroclubul București</a></li>
          <li><span class="ap-org-logo"><img src="/static/img/astroian_observatory.png" alt="" width="77" height="42" decoding="async" loading="lazy"></span>Astroian Observatory</li>
          <li><a href="https://www.sternfreunde.berlin/" target="_blank" rel="noopener"><span class="ap-org-logo"><img src="/static/img/sternfreunde_berlin.png" alt="" width="44" height="46" decoding="async" loading="lazy"></span>Sternfreunde Berlin</a></li>
          <li><span class="ap-org-logo"><img src="/static/img/astroclubul_iasi.png" alt="" width="46" height="46" decoding="async" loading="lazy" class="ap-org-round"></span>AstroClubul Iași</li>
          <li><a href="https://povesticustele.wordpress.com/" target="_blank" rel="noopener"><span class="ap-org-logo"><img src="/static/img/povesti_cu_stele_emblem.png" alt="" width="45" height="42" decoding="async" loading="lazy"></span>Povești cu Stele</a></li>
          <li><a href="http://stardustlc.wordpress.com/" target="_blank" rel="noopener"><span class="ap-org-logo"><img src="/static/img/zeta_aquarii_observatory.png" alt="" width="45" height="46" decoding="async" loading="lazy"></span>Zeta Aquarii Observatory</a></li>
        </ul>
      </section>

      <section class="ap-sec">
        <h3>The network in numbers</h3>
        <div id="about-network-stats" class="ap-stats" aria-live="polite">Loading coverage data&hellip;</div>
      </section>

      <p class="ap-license">&copy; 2013&ndash;2026 ROVIMEN. Source code under the <a href="https://www.gnu.org/licenses/gpl-3.0.html" target="_blank" rel="noopener">GNU General Public License v3.0</a>.</p>
    </div>`,
  },
};

/* The panel is a modal dialog: role, aria-modal and the title label are set
   here so every template's #app-panel gets them, Escape closes it, and
   _modalOpen/_modalClose move focus into it and back to the opener. Pages
   that do not ship the panel markup get it built on first use, so the
   footer's "About ROVIMEN" works everywhere. */
function _ensureAppPanel() {
  let wrapEl = document.getElementById('app-panel');
  if (!wrapEl) {
    wrapEl = document.createElement('div');
    wrapEl.id = 'app-panel';
    wrapEl.className = 'app-panel';
    wrapEl.setAttribute('aria-hidden', 'true');
    wrapEl.innerHTML = `<div class="app-panel-inner">
        <div class="app-panel-header">
          <h2 id="app-panel-title" class="app-panel-title"></h2>
          <button type="button" class="cam-modal-close">Close</button>
        </div>
        <div id="app-panel-body"></div>
      </div>`;
    wrapEl.addEventListener('click', e => appPanelClose(e));
    wrapEl.querySelector('.cam-modal-close').addEventListener('click', () => appPanelClose(null));
    document.body.appendChild(wrapEl);
  }
  if (!wrapEl.dataset.a11y) {
    wrapEl.dataset.a11y = '1';
    wrapEl.setAttribute('role', 'dialog');
    wrapEl.setAttribute('aria-modal', 'true');
    if (document.getElementById('app-panel-title')) wrapEl.setAttribute('aria-labelledby', 'app-panel-title');
    const closeBtn = wrapEl.querySelector('.cam-modal-close');
    if (closeBtn && !closeBtn.hasAttribute('aria-label')) closeBtn.setAttribute('aria-label', 'Close');
    if (closeBtn && !closeBtn.hasAttribute('type')) closeBtn.setAttribute('type', 'button');
    wrapEl.addEventListener('keydown', e => {
      if (e.key === 'Escape' && wrapEl.classList.contains('open')) {
        e.stopPropagation();
        appPanelClose(null);
      }
    });
  }
  return wrapEl;
}

export function openAppPanel(id) {
  document.getElementById('logo-dropdown')?.classList.remove('open');
  document.getElementById('logo-btn')?.setAttribute('aria-expanded', 'false');
  const panel = _APP_PANELS[id];
  if (!panel) return;
  const wrapEl  = _ensureAppPanel();
  const titleEl = document.getElementById('app-panel-title');
  const bodyEl  = document.getElementById('app-panel-body');
  if (!titleEl || !bodyEl || !wrapEl) return;
  titleEl.textContent = panel.title;
  bodyEl.innerHTML = panel.html;
  _modalOpen(wrapEl);
  if (id === 'about') _loadNetworkStats();
}

async function _loadNetworkStats() {
  const el = document.getElementById('about-network-stats');
  if (!el) return;
  try {
    const r = await fetch('/api/network-stats');
    if (!r.ok) { el.textContent = 'Could not load network statistics. Reopen this panel to try again.'; return; }
    const d = await r.json();
    const s = d.network_summary || {};
    const dbl = d.double_station_coverage_km2 || {};
    const fmt = (v) => v != null ? Math.round(v).toLocaleString() : '—';
    const perStation = d.per_station || {};
    const stRows = Object.entries(perStation).map(([k, v]) =>
      `<tr><td class="ap-code" translate="no">${escHtml(k)}</td>` +
      `<td>${escHtml(v.label)}</td>` +
      `<td class="ap-num">${v.cameras}</td>` +
      `<td class="ap-num">${v.cameras_with_platepar}</td>` +
      `<td class="ap-num">${fmt(v.covered_area_km2)}</td></tr>`
    ).join('');
    el.innerHTML = `
      <dl class="ap-figures">
        <div><dt>Stations</dt><dd>${s.total_stations}</dd></div>
        <div><dt>Cameras</dt><dd>${s.total_cameras}</dd></div>
        <div><dt>Calibrated cameras</dt><dd>${s.cameras_with_platepar}</dd></div>
        <div><dt>Sky covered at 100&nbsp;km</dt><dd class="ap-blue">${fmt(d.total_covered_area_km2)}&nbsp;km&sup2;</dd></div>
        <div><dt>Seen by two stations at 100&nbsp;km</dt><dd class="ap-green">${fmt(dbl['100km'])}&nbsp;km&sup2;</dd></div>
        <div><dt>Sporadic meteors expected (&gt;+4.5 mag)</dt><dd class="ap-yellow">${fmt(d.expected_meteoric_flux_per_hour)}/hr</dd></div>
      </dl>
      <p class="ap-note">Seen by two stations at 25&nbsp;km: <b>${fmt(dbl['25km'])}&nbsp;km&sup2;</b>, at 70&nbsp;km: <b>${fmt(dbl['70km'])}&nbsp;km&sup2;</b>. Atmospheric volume watched (25&ndash;130&nbsp;km): <b>${fmt(d.atmospheric_volume_km3)}&nbsp;km&sup3;</b>.</p>
      <details>
        <summary>Coverage per station at 100&nbsp;km</summary>
        <table class="ap-table">
          <thead><tr>
            <th scope="col">Station</th><th scope="col">Place</th>
            <th scope="col" class="ap-num">Cameras</th>
            <th scope="col" class="ap-num">Calibrated</th>
            <th scope="col" class="ap-num">Area (km&sup2;)</th>
          </tr></thead>
          <tbody>${stRows}</tbody>
        </table>
      </details>
      <p class="ap-foot">Computed from the cameras' calibrations on a 10&nbsp;km grid. Sporadic rate scaled to a +4.5 limiting magnitude from ZHR ~10/hr (Rendtel 2006). <a href="/network">See the coverage on the map</a>.</p>`;
  } catch(e) {
    el.textContent = 'Could not load network statistics. Reopen this panel to try again.';
  }
}

export function appPanelClose(e) {
  const wrapEl = document.getElementById('app-panel');
  if (!wrapEl) return;
  if (e && e.target !== wrapEl) return;
  _modalClose(wrapEl);
}

/* ─────────────────────────────────────────
   Modal accessibility — shared helpers
   ─────────────────────────────────────────
   Lives in dashboard-common.js (loaded by every page) so the same modal
   open/close path is available from any template — overview.html,
   events.html, dashboard.html — and from every lazy bundle.

   _modalOpen(el):  save the currently-focused element so we can restore it
                    on close, flip aria-hidden off, mark the modal `.open`,
                    lock body scroll (so the page beneath doesn't slide on
                    mobile swipe), move keyboard focus to the modal, and
                    install a Tab keydown handler that traps focus inside
                    the dialog.
   _modalClose(el): reverse all of the above, unlock body scroll, and
                    restore focus to the element that originally opened
                    the modal.

   Tab trapping loops focus between the first and last tabbable descendant
   of the modal. Shift+Tab on the first wraps to the last; Tab on the last
   wraps to the first. Buttons, links, inputs, selects, textareas and
   elements with explicit tabindex >= 0 are considered tabbable.

   Body scroll-lock uses a `position: fixed` strategy on <body> (instead of
   `overflow: hidden`) because iOS Safari ignores `overflow: hidden` on
   <body> when there's any momentum scroll. We save the scroll Y on first
   lock and restore it on final unlock so the user lands back where they
   were before the modal opened. A counter handles nested modals: the body
   stays locked as long as at least one modal is open.
───────────────────────────────────────── */
const _MODAL_TABBABLE_SEL = [
  'a[href]:not([disabled])',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"]):not([disabled])',
].join(',');

function _modalTabbables(el) {
  return Array.from(el.querySelectorAll(_MODAL_TABBABLE_SEL))
    .filter(n => n.offsetParent !== null || n === document.activeElement);
}

function _modalTrapHandler(el) {
  return function (e) {
    if (e.key !== 'Tab') return;
    const items = _modalTabbables(el);
    if (!items.length) { e.preventDefault(); el.focus(); return; }
    const first = items[0];
    const last  = items[items.length - 1];
    const active = document.activeElement;
    if (e.shiftKey) {
      if (active === first || !el.contains(active)) { e.preventDefault(); last.focus(); }
    } else {
      if (active === last || !el.contains(active))  { e.preventDefault(); first.focus(); }
    }
  };
}

if ('scrollRestoration' in history) history.scrollRestoration = 'manual';

let _openModalCount = 0;
let _popstateClosing = false;

export function _lockBodyScroll() {
  if (_openModalCount === 0) {
    const y = window.scrollY || document.documentElement.scrollTop || 0;
    document.body.dataset.lockedScrollY = String(y);
    document.body.style.position = 'fixed';
    document.body.style.top = `-${y}px`;
    document.body.style.left = '0';
    document.body.style.right = '0';
    document.body.style.width = '100%';
    document.body.style.overflow = 'hidden';
  }
  _openModalCount++;
}

export function _unlockBodyScroll() {
  _openModalCount = Math.max(0, _openModalCount - 1);
  if (_openModalCount === 0) {
    const y = parseInt(document.body.dataset.lockedScrollY || '0', 10);
    document.body.style.position = '';
    document.body.style.top = '';
    document.body.style.left = '';
    document.body.style.right = '';
    document.body.style.width = '';
    document.body.style.overflow = '';
    delete document.body.dataset.lockedScrollY;
    window.scrollTo(0, y);
  }
}

export function _modalOpen(el) {
  if (!el) return;
  if (el.classList.contains('open')) return;
  el._opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  el.setAttribute('aria-hidden', 'false');
  el.classList.add('open');
  _lockBodyScroll();
  // Push a history entry on the first modal so the mobile back gesture
  // closes the modal instead of navigating away from the page. A URL write
  // still debouncing (a camera pick, a slider drag) goes into the page's
  // entry first, not the modal's, so Back returns to that view.
  if (_openModalCount === 1) {
    for (const sync of [..._pendingUrlSyncs]) sync.flush();
    history.pushState({ _rovModal: true }, '');
  }
  if (!el.hasAttribute('tabindex')) el.setAttribute('tabindex', '-1');
  const items = _modalTabbables(el);
  (items[0] || el).focus({ preventScroll: true });
  el._trapHandler = _modalTrapHandler(el);
  el.addEventListener('keydown', el._trapHandler);
}

export function _modalClose(el) {
  if (!el) return;
  if (!el.classList.contains('open')) return;
  el.classList.remove('open');
  _unlockBodyScroll();
  el.setAttribute('aria-hidden', 'true');
  if (el._trapHandler) {
    el.removeEventListener('keydown', el._trapHandler);
    el._trapHandler = null;
  }
  // Pop the history entry we pushed in _modalOpen when the last modal
  // closes — but only when the close was programmatic (button click,
  // Escape key). If popstate triggered the close, the entry is already
  // gone and calling history.back() would navigate away.
  if (_openModalCount === 0 && !_popstateClosing) {
    if (history.state && history.state._rovModal) {
      // Delay one frame so the scroll-restore paint settles before the
      // back-navigation fires. Without this, some browsers briefly show
      // scroll=0 between the popstate and our scrollTo call.
      requestAnimationFrame(() => {
        if (history.state && history.state._rovModal) history.back();
      });
    }
  }
  const opener = el._opener;
  el._opener = null;
  if (opener && typeof opener.focus === 'function' && document.body.contains(opener)) {
    opener.focus({ preventScroll: true });
  }
}

// Expose to other bundles / inline template scripts that don't share
// lexical scope with this file.

/* ─────────────────────────────────────────
   Close-on-route-change helper
   ─────────────────────────────────────────
   Any page navigation (station switch, tab switch, browser back) should
   clear out modals that were left open — otherwise a video player or
   plot can persist on top of fresh content. Single source of truth so
   the various switch sites all dismiss the same set of modal classes.
───────────────────────────────────────── */
export function _closeAllOpenModals() {
  // Covers every modal class that uses the shared .open toggle:
  //   .cam-modal       — live cam, RMS plot, dashboard stack-modal
  //   .vm-backdrop     — shared VideoModal component (VDB, Archive, Highlights, Overview, Events)
  //   .mdm-backdrop    — MultiDetModal (Events multi-station, VDB All Cams)
  //   .app-panel       — Stats / GMN / About app panels
  //   .ov-plot-modal   — overview plot zoom modal
  //   .spectro-modal   — Spectro tab image lightbox
  // Leaflet popups (.gmn-popup) are not full-screen modals — they're
  // dismissed by clicking the map or pressing Esc; intentionally skipped.
  //
  // Several modals own resources beyond the .open class (video buffering,
  // wheel/mousedown listeners, intervalled refreshes). Route through the
  // page-local "real close" function when one exists so those tear down
  // cleanly — falling back to bare _modalClose for plain dialogs.

  // Close all VideoModal instances first (they own their own teardown logic).
  if (typeof window._VideoModalCloseAll === 'function') window._VideoModalCloseAll();

  const dispatchers = {
    'cam-modal':      () => typeof closeCamModal       === 'function' ? closeCamModal()  : _modalClose(document.getElementById('cam-modal')),
    'stack-modal':    () => typeof stackCloseModal     === 'function' ? stackCloseModal() : _modalClose(document.getElementById('stack-modal')),
    'ov-plot-modal':  () => typeof ovPlotClose         === 'function' ? ovPlotClose()     : _modalClose(document.getElementById('ov-plot-modal')),
    'app-panel':      () => typeof appPanelClose       === 'function' ? appPanelClose()   : _modalClose(document.getElementById('app-panel')),
    'ov-live-modal':  () => typeof closeLiveModal      === 'function' ? closeLiveModal()  : _modalClose(document.getElementById('ov-live-modal')),
    'spectro-modal':  () => typeof window.spectroLightboxClose === 'function' ? window.spectroLightboxClose() : _modalClose(document.getElementById('spectro-modal')),
  };
  const open = document.querySelectorAll(
    '.cam-modal.open, .app-panel.open, ' +
    '.ov-plot-modal.open, .ov-live-modal-wrap.open, ' +
    '.spectro-modal.open'
  );
  open.forEach(el => {
    const dispatch = dispatchers[el.id];
    try {
      if (dispatch) dispatch();
      else _modalClose(el);
    } catch (e) {
      // Last-resort fallback so a broken page-local close doesn't leave
      // a modal stuck on top of fresh content.
      _modalClose(el);
    }
  });
}

// Browser back/forward (and the system back gesture on mobile) should
// dismiss any open modal instead of navigating away. The _popstateClosing
// flag tells _modalClose not to call history.back() again (the entry was
// already consumed by the back gesture).
window.addEventListener('popstate', () => {
  if (_openModalCount > 0) {
    _popstateClosing = true;
    _closeAllOpenModals();
    _popstateClosing = false;
  }
});

/* ─────────────────────────────────────────
   HTML escape — used by every page that renders user-controlled strings.
   Escapes both element-text and attribute-value metacharacters so the same
   helper is safe in either context. Returns "" for null/undefined so callers
   can drop the `?? ''` boilerplate at every interpolation site.
   Canonical implementation; admin.html / events.html previously had their
   own copies (deduped 2026-05-25 as part of the XSS sweep, P1-1).
───────────────────────────────────────── */
export function escHtml(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/* ─────────────────────────────────────────
   Service state
───────────────────────────────────────── */
/** True when a /api/status service value means "the work is running".
 *  The station API reports "active" for a service running under an active
 *  systemd unit and "active (unmanaged)" when it found the process running
 *  without one (legacy nohup/cron installs, or a unit whose name it could not
 *  resolve). Both are up; only "inactive" / "failed" are not. Objects with an
 *  {active|state|running} field are accepted for forward compatibility. */
export function serviceIsUp(val) {
  if (typeof val === 'string') return val.startsWith('active');
  if (val && typeof val === 'object') {
    return val.active === true || val.running === true ||
           (typeof val.state === 'string' && val.state.startsWith('active'));
  }
  return false;
}

/** CSS state suffix for a service value: 'active' | 'unmanaged' | 'inactive'
 *  | 'failed' | 'unknown'. Keeps the dot/border vocabulary in one place. */
export function serviceStateClass(val) {
  if (typeof val !== 'string') return serviceIsUp(val) ? 'active' : 'unknown';
  if (val === 'active') return 'active';
  if (val.startsWith('active')) return 'unmanaged';
  if (val === 'inactive' || val === 'failed') return val;
  return 'unknown';
}

/* ─────────────────────────────────────────
   Nights and times of day. An RMS night runs noon to noon UTC and is
   named after the evening it starts (YYYYMMDD).
───────────────────────────────────────── */

/** RMS night of a UTC instant: 02:00 on the 19th is the night of the 18th. */
export function rmsNightOf(t = new Date()) {
  const d = new Date(t.getTime());
  if (d.getUTCHours() < 12) d.setUTCDate(d.getUTCDate() - 1);
  return d.toISOString().slice(0, 10).replace(/-/g, '');
}

/** "20261020" → "Night of 20 October" (the year added when it is not this one). */
export function nightTitle(date) {
  const d = new Date(`${date.slice(0, 4)}-${date.slice(4, 6)}-${date.slice(6, 8)}T12:00:00Z`);
  const sameYear = d.getUTCFullYear() === new Date().getUTCFullYear();
  return 'Night of ' + d.toLocaleDateString('en-GB', { day: 'numeric', month: 'long', ...(sameYear ? {} : { year: 'numeric' }), timeZone: 'UTC' });
}

/** A time field's "HH:MM" (or "HHMM" / "H:MM") → minutes of day, or null if
 *  invalid. 24:00 is accepted as the end of a window. */
export function parseHHMM(str) {
  if (str == null) return null;
  const s = String(str).trim().replace(/\s/g, '');
  const m = /^(\d{1,2}):(\d{2})$/.exec(s) || /^(\d{1,2})(\d{2})$/.exec(s);
  if (!m) return null;
  const h = parseInt(m[1], 10);
  const mn = parseInt(m[2], 10);
  if (h < 0 || h > 24 || mn < 0 || mn > 59 || (h === 24 && mn !== 0)) return null;
  return h * 60 + mn;
}

/* ─────────────────────────────────────────
   UTC Clock + Next RMS Capture
───────────────────────────────────────── */

/** Format a Date as an unambiguous UTC wall-clock string, e.g.
 *  "2026-08-03 20:14:07 UTC". Pure (no DOM, no now()) so it is unit-testable
 *  and every page renders the identical label. Invalid input yields "—".
 *  @param {Date} date
 *  @returns {string} */
const SHORT_MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
/** "20261020" → "20 Oct": the night pickers' label (the value stays YYYYMMDD). */
export function shortNightLabel(d) {
  const s = String(d || '');
  return `${parseInt(s.slice(6, 8), 10)} ${SHORT_MONTHS[parseInt(s.slice(4, 6), 10) - 1] || ''}`;
}

export function formatUtcClock(date) {
  if (!(date instanceof Date) || Number.isNaN(date.getTime())) return '—';
  const p = (n) => String(n).padStart(2, '0');
  const Y = date.getUTCFullYear();
  const Mo = p(date.getUTCMonth() + 1);
  const D = p(date.getUTCDate());
  const h = p(date.getUTCHours());
  const m = p(date.getUTCMinutes());
  const s = p(date.getUTCSeconds());
  return `${Y}-${Mo}-${D} ${h}:${m}:${s} UTC`;
}

// Format a UTC hour-of-day (may be < 0 or > 24) as "HH:MM", wrapping to a day.
export function fmtCaptureUTC(utcHours) {
  const totalMin = Math.round(((utcHours % 24) + 24) % 24 * 60);
  return `${String(Math.floor(totalMin / 60)).padStart(2,'0')}:${String(totalMin % 60).padStart(2,'0')}`;
}

// Pure solar geometry for the RMS capture window. Returns null when the sun
// never crosses the threshold (polar day/night), otherwise the sunset/sunrise
// UTC hours, whether `now` is inside the window, and the formatted label.
//
// Threshold: civil twilight (sun at -6° elevation). RMS does not gate capture
// on nautical twilight — the earlier -12° value here came from misreading
// `cf3` in the station config (a sky-conditions flag, not a start elevation).
// Civil twilight matches observed camera start times, which fire 25-35 min
// earlier than the -12° model predicted at Romanian latitudes (#495).
export function computeCaptureWindow(lat, lon, now = new Date()) {
  if (lat == null || lon == null) return null;
  const dayOfYear = Math.floor((now - new Date(Date.UTC(now.getUTCFullYear(), 0, 1))) / 86400000) + 1;
  const declRad = 23.45 * Math.PI / 180 * Math.sin((360 / 365 * (dayOfYear - 81)) * Math.PI / 180);
  const latRad = lat * Math.PI / 180;
  const elevRad = -6 * Math.PI / 180; // civil twilight (approximation of RMS start)
  const cosH = (Math.sin(elevRad) - Math.sin(latRad) * Math.sin(declRad)) / (Math.cos(latRad) * Math.cos(declRad));
  if (Math.abs(cosH) > 1) return null; // sun never reaches the threshold today
  const H = Math.acos(cosH) * 180 / Math.PI;
  const solarNoonUTC = 12 - lon / 15;
  const sunsetUTC  = solarNoonUTC + H / 15;
  const sunriseUTC = solarNoonUTC - H / 15;

  // Capture runs from evening sunset to next-morning sunrise, wrapping
  // midnight. Wrap all values into [0,24) and test membership accordingly.
  const wrap = h => ((h % 24) + 24) % 24;
  const nowH = now.getUTCHours() + now.getUTCMinutes() / 60 + now.getUTCSeconds() / 3600;
  const setW = wrap(sunsetUTC);
  const riseW = wrap(sunriseUTC);
  const active = setW > riseW
    ? (nowH >= setW || nowH < riseW)   // window wraps midnight (normal night)
    : (nowH >= setW && nowH < riseW);  // window within a single UTC day

  const label = active
    ? `<span class="hdr-clock-label">Capture window active &middot; ends</span> <span class="hdr-clock">${fmtCaptureUTC(sunriseUTC)} UTC</span>`
    : `<span class="hdr-clock-label">Next RMS capture window:</span> <span class="hdr-clock">${fmtCaptureUTC(sunsetUTC)} &rarr; ${fmtCaptureUTC(sunriseUTC)} UTC</span>`;

  return { sunsetUTC, sunriseUTC, active, label };
}

// The header on every page shows the UTC clock and tonight's RMS capture
// window, computed for the centre of Romania (the fleet spans about +-1 h of
// it; each station page also shows its own window in the station toolbar).
const NETWORK_CENTRE = { lat: 45.94, lon: 24.97 };

export function networkRmsLabel(now = new Date()) {
  const win = computeCaptureWindow(NETWORK_CENTRE.lat, NETWORK_CENTRE.lon, now);
  if (!win) return '';
  return win.active
    ? `RMS recording · until ${fmtCaptureUTC(win.sunriseUTC)} UTC`
    : `RMS starts ${fmtCaptureUTC(win.sunsetUTC)} UTC`;
}

/** Tonight's sky in the header strip (templates/_nav.html .js-night-bar):
 *  positions on a 12:00 → 12:00 UTC axis, the network's night-date
 *  convention, so the night sits in the middle of the bar. Sets CSS custom
 *  properties only; the gradient and the "now" marker are drawn in CSS. */
function paintNightBars(now) {
  const bars = document.querySelectorAll('.js-night-bar');
  if (!bars.length) return;
  const win = computeCaptureWindow(NETWORK_CENTRE.lat, NETWORK_CENTRE.lon, now);
  const pos = (h) => ((((h - 12) % 24) + 24) % 24) / 24 * 100;
  const nowH = now.getUTCHours() + now.getUTCMinutes() / 60 + now.getUTCSeconds() / 3600;
  bars.forEach((el) => {
    if (win) {
      el.style.setProperty('--set', `${pos(win.sunsetUTC).toFixed(2)}%`);
      el.style.setProperty('--rise', `${pos(win.sunriseUTC).toFixed(2)}%`);
    }
    el.style.setProperty('--now', `${pos(nowH).toFixed(2)}%`);
    // On a narrow phone the bar only gets what the clock and RMS text leave;
    // below ~36 px it reads as a glitch rather than a sky, so hide it.
    el.classList.toggle('is-cramped', el.getBoundingClientRect().width < 36);
  });
}

(function startUTCClock() {
  function tick() {
    // A page may carry more than one clock element (e.g. a full-date header
    // plus a compact mobile strip), so paint every #utc-clock rather than the
    // first match only.
    const now = new Date();
    const text = formatUtcClock(now);
    document.querySelectorAll('#utc-clock, .js-utc-clock').forEach((el) => { el.textContent = text; });
    const rms = networkRmsLabel(now);
    document.querySelectorAll('.js-rms-window').forEach((el) => {
      if (el.textContent !== rms) el.textContent = rms;
      el.hidden = !rms;
    });
    paintNightBars(now);
  }
  tick();
  setInterval(tick, 1000);
})();

// ── Toast notification ─────────────────────────────────────────────────────────

/** Say so when a trimmed download came back whole: /shortclip/ answers with
 *  X-Clip-Trimmed: 0 when it could only serve the untrimmed clip. */
export function noteUntrimmed(resp) {
  try {
    if (resp && resp.headers && resp.headers.get('X-Clip-Trimmed') === '0') {
      _toast('Could not trim this clip here, so the whole clip was downloaded', 'info');
    }
  } catch (e) { /* never block a download on the notice */ }
}

export function _toast(msg, type) {
  const t = document.createElement('div');
  t.className = 'toast toast-' + (type || 'info');
  t.setAttribute('role', type === 'error' ? 'alert' : 'status');
  t.textContent = msg;
  Object.assign(t.style, {
    position: 'fixed', bottom: '24px', left: '50%', transform: 'translateX(-50%)',
    padding: '10px 20px', borderRadius: '6px', zIndex: '99999', fontSize: '13px',
    color: '#fff', background: type === 'error' ? '#c33' : '#333', opacity: '0.95',
    pointerEvents: 'none', transition: 'opacity 0.3s',
  });
  document.body.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 400); }, 3600);
}

// ── Burger menu ───────────────────────────────────────────────────────────────

(function initBurgerMenu() {
  // The burger and its panel are rendered by templates/_nav.html; this only
  // opens and closes them. Guarded so a page that evaluates this module twice
  // (two different URLs for the same file) cannot bind it twice.
  const burger = document.querySelector('.nav-burger');
  const panel = document.getElementById('nav-panel');
  if (!burger || !panel || burger.dataset.bound) return;
  burger.dataset.bound = '1';

  function setOpen(open) {
    if (open) {
      // Drop the panel exactly under whichever header it belongs to; header
      // heights differ per page (the station page has a toolbar row).
      const hdr = burger.parentElement.getBoundingClientRect();
      panel.style.top = `${Math.round(hdr.bottom)}px`;
    }
    panel.hidden = !open;
    burger.classList.toggle('is-open', open);
    burger.setAttribute('aria-expanded', String(open));
    burger.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
    document.documentElement.classList.toggle('nav-panel-open', open);
  }
  const close = () => setOpen(false);

  burger.addEventListener('click', (e) => { e.stopPropagation(); setOpen(panel.hidden); });
  panel.addEventListener('click', (e) => { if (e.target.closest('a')) close(); });
  document.addEventListener('click', (e) => {
    if (!panel.hidden && !panel.contains(e.target) && !burger.contains(e.target)) close();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || panel.hidden) return;
    close();
    burger.focus();
  });
  // Rotating to landscape / resizing past the breakpoint hides the burger;
  // don't leave its panel stranded open.
  window.matchMedia('(min-width: 1025px)').addEventListener('change', (m) => { if (m.matches) close(); });
})();

// ── Sticky offsets ──────────────────────────────────────────────────────────
// The header (and a page's tab row) are sticky; rows that stick beneath them
// read their heights from --site-hdr-h / --site-tabs-h (dashboard.css), kept
// current here as the header wraps, the strip changes or the window resizes.
(function publishStickyOffsets() {
  const logo = document.querySelector('.nav-logo');
  const hdr = logo && logo.parentElement;
  if (!hdr || typeof ResizeObserver === 'undefined' || hdr.dataset.offsetsBound) return;
  hdr.dataset.offsetsBound = '1';
  const root = document.documentElement.style;
  const tabs = document.querySelector('.sub-tabs, .ev-tabs, .adm-tabs');
  const ro = new ResizeObserver(() => {
    root.setProperty('--site-hdr-h', `${Math.round(hdr.getBoundingClientRect().height)}px`);
    if (tabs) root.setProperty('--site-tabs-h', `${Math.round(tabs.getBoundingClientRect().height)}px`);
  });
  ro.observe(hdr);
  if (tabs) ro.observe(tabs);
})();

// ── The session, asked once per page load ───────────────────────────────────
// The header, the page's own init, the clip controls, the basket and the Hall
// of Fame toggle all await this one /api/auth/status request. It writes the
// answer into state (AUTH_USER, IS_ADMIN, USER_ROLE, USER_STATIONS) before any
// awaiting code runs, then resolves the payload ({user, role, admin, stations});
// null when the request fails.
let _authPromise = null;

export function fetchAuthStatus() {
  if (!_authPromise) {
    _authPromise = fetch('/api/auth/status')
      .then(r => r.json())
      .then(a => {
        state.AUTH_USER = a.user || null;
        state.IS_ADMIN = !!a.admin;
        state.CAN_HIDE = !!a.can_hide;
        if (ACCOUNT_ROLES.includes(a.role)) state.USER_ROLE = a.role;
        if (Array.isArray(a.stations)) state.USER_STATIONS = a.stations;
        return a;
      })
      .catch(() => null);
  }
  return _authPromise;
}

// ── Account in the header (templates/_nav.html #nav-auth + #nav-panel-auth) ──
// One renderer for every page, replacing the per-page Login/Logout copies.
// Desktop: a Login button, or the user's chip with Logout in its menu. Phone
// menu: "Signed in as" + Logout, or a full-width Login. It also shows or hides
// the admin-only nav links for every page.
(function initNavAuth() {
  const bar = document.getElementById('nav-auth');
  const panel = document.getElementById('nav-panel-auth');
  if ((!bar && !panel) || (bar && bar.dataset.bound)) return;
  if (bar) bar.dataset.bound = '1';
  const next = encodeURIComponent(location.pathname + location.search);
  fetchAuthStatus().then((a) => {
    if (!a) return;
    setAdminNav(!!a.admin);
    const user = a.user;
    if (!user) {
      if (bar) bar.innerHTML = `<a class="hc hc-primary" href="/login?next=${next}">Log in</a>`;
      if (panel) panel.innerHTML = `<a class="hc hc-primary nav-panel-login" href="/login?next=${next}">Log in</a>`;
      return;
    }
    const name = escHtml(user);
    const initial = escHtml(user.charAt(0).toUpperCase());
    const role = a.role && a.role !== 'guest' ? escHtml(a.role) : '';
    if (bar) {
      bar.innerHTML = `
        <details class="nav-user">
          <summary class="hc nav-user-btn" title="Signed in as ${name}">
            <span class="nav-avatar" aria-hidden="true">${initial}</span><span class="nav-user-name">${name}</span>
          </summary>
          <div class="nav-user-menu">
            <p class="nav-user-who">Signed in as <b>${name}</b>${role ? `<span>${role}</span>` : ''}</p>
            <a href="/alerts" class="nav-tab">Fireball alerts</a>
            <a href="/logout" class="nav-tab">Log out</a>
          </div>
        </details>`;
      const det = bar.querySelector('details');
      document.addEventListener('click', (e) => { if (det.open && !det.contains(e.target)) det.open = false; });
      det.addEventListener('keydown', (e) => { if (e.key === 'Escape') det.open = false; });
    }
    if (panel) {
      panel.innerHTML = `
        <div class="nav-panel-acct">
          <span class="nav-avatar" aria-hidden="true">${initial}</span>
          <span class="nav-panel-who"><b>${name}</b>${role ? `<span>${role}</span>` : ''}</span>
          <a href="/alerts" class="hc">Fireball alerts</a>
          <a href="/logout" class="hc">Log out</a>
        </div>`;
    }
  }).catch(() => {});
})();

// The nav's skip link targets #main; a <main> without an id gets it here so
// the link always lands on the content. A <main> that already has an id keeps
// it (page scripts look it up: renaming #sh-page-wrap left the Showers page
// empty) and the link points at that id instead.
(function ensureSkipTarget() {
  const link = document.querySelector('.skip-link');
  if (!link || document.getElementById('main')) return;
  const main = document.querySelector('main');
  if (!main) return;
  if (main.id) link.setAttribute('href', `#${main.id}`);
  else main.id = 'main';
  if (!main.hasAttribute('tabindex')) main.setAttribute('tabindex', '-1');
})();

// ── Tools menu (templates/_nav.html <details id="nav-tools">) ────────────────
// A <details> menu stays open until its summary is clicked again; close it
// on Escape (focus back on the summary), on a click outside, and when focus
// leaves it, like any other menu.

(function initToolsMenu() {
  const tools = document.getElementById('nav-tools');
  if (!tools) return;
  const summary = tools.querySelector('summary');
  const close = () => { tools.open = false; };
  if (summary) {
    summary.setAttribute('aria-haspopup', 'true');
    const sync = () => summary.setAttribute('aria-expanded', String(tools.open));
    sync();
    tools.addEventListener('toggle', sync);
  }
  document.addEventListener('click', e => {
    if (tools.open && !tools.contains(e.target)) close();
  });
  tools.addEventListener('keydown', e => {
    if (e.key !== 'Escape' || !tools.open) return;
    e.stopPropagation();
    close();
    summary?.focus();
  });
  tools.addEventListener('focusout', e => {
    if (tools.open && e.relatedTarget && !tools.contains(e.relatedTarget)) close();
  });
})();

/* Padlock icons as inline SVG. Rendered instead of the 🔒/🔓 emoji
   (U+1F512/U+1F513) because those codepoints only show up when the user's
   OS ships a colour-emoji font — many minimal Linux desktops and locked-down
   corporate machines don't, so the emoji rendered as tofu or nothing. SVG
   renders identically everywhere and inherits the button colour via
   `currentColor`. Sized in `em` so it scales with the host element's font-size. */
export const LOCK_ICON =
  '<svg class="lock-ico" viewBox="0 0 24 24" width="1em" height="1em" fill="none" ' +
  'stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" ' +
  'aria-hidden="true"><rect x="4" y="10.5" width="16" height="11" rx="2.2"/>' +
  '<path d="M7.5 10.5V7a4.5 4.5 0 0 1 9 0v3.5"/></svg>';
export const UNLOCK_ICON =
  '<svg class="lock-ico" viewBox="0 0 24 24" width="1em" height="1em" fill="none" ' +
  'stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" ' +
  'aria-hidden="true"><rect x="4" y="10.5" width="16" height="11" rx="2.2"/>' +
  '<path d="M7.5 10.5V7a4.5 4.5 0 0 1 8.5-2"/></svg>';

/** Roles a real account can hold. Mirrors `_ACCOUNT_ROLES` in auth.py; anything
 *  else (notably 'guest') is an anonymous or unrecognised session. */
const ACCOUNT_ROLES = ['admin', 'host', 'visitor', 'press'];

/** True if the current session may lock clips on `host`. Mirrors the server-side
 *  `@require_station_lock` gate in auth.py: locking is fleet-wide for every
 *  signed-in account, so a witness at another station can pin a fireball chunk
 *  before the retention janitor takes it. Station ownership no longer narrows
 *  this — it only gates unlocking, which the dashboard never requests. Guests
 *  get 401 from the server, so they get no button. `_host` is unread but kept
 *  in the signature: every call site already passes the host_key the request
 *  goes to, so re-scoping the grant per station stays a one-line change here. */
export function canToggleLock(_host) {
  return ACCOUNT_ROLES.includes(state.USER_ROLE);
}

/** Reset the memoised session fetch — test hook only. */
export function _resetRoleCacheForTests() { _authPromise = null; }

/** Resolve the session role for self-gating clip controls. dashboard-station.js
 *  waits for the session before it renders, but pages that open the shared
 *  video modal on their own schedule (Events, Highlights) could ask before it
 *  landed and read the default 'guest', which silently hid the lock and
 *  process buttons there. When the role is not already known, wait for the
 *  page's one fetchAuthStatus request, which writes it into state. */
export function resolveUserRole() {
  if (ACCOUNT_ROLES.includes(state.USER_ROLE)) return Promise.resolve(state.USER_ROLE);
  return fetchAuthStatus().then(() => state.USER_ROLE);
}

/** POST a lock request for one clip and return the station's {locked, lock_type}
 *  response. Shared by the VDB grid lock toggle and the video-modal lock button
 *  so both speak to the one-way `/api/lock` proxy identically. Throws with a
 *  human-readable message on 401/403/HTML/error bodies. `host` is the host_key,
 *  `camera` the camera code; `filename` must be the `.mkv` chunk (server-enforced). */
export async function lockClip(host, camera, date, filename) {
  const resp = await fetch(
    `/api/lock/${host}/${camera}/${date}/${encodeURIComponent(filename)}`,
    { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ locked: true }) }
  );
  if (!resp.ok) {
    const ct = resp.headers.get('content-type') || '';
    if (ct.includes('text/html') || resp.status === 401) {
      throw new Error(`Session expired (${resp.status}). Please refresh the page.`);
    }
    if (resp.status === 403) throw new Error('You do not have access to this station.');
    const body = await resp.text();
    throw new Error(body.length > 200 ? body.slice(0, 200) + '…' : body);
  }
  return resp.json();   // { locked, lock_type }
}

// Clips already sent to /api/keep in this page load, by "host/cam/date/file".
const _keptClips = new Map();

/** Keep a clip a signed-in user plays: POST /api/keep, which records it so the
 *  Storage Box janitor never prunes it and locks it on its station, which
 *  uploads it now. Once per clip per page load; anonymous sessions and clips
 *  without a full identity are skipped. Resolves to the server's
 *  {kept, on_station, locked, lock_type}, or null when skipped or failed.
 *  Never throws: playback must not depend on it. */
export function keepClip(host, camera, date, filename) {
  const ymd = String(date || '').replace(/-/g, '');
  if (!(host && camera && /^\d{8}$/.test(ymd) && /_color(\.details)?\.(mkv|mp4)$/i.test(filename || ''))) {
    return Promise.resolve(null);
  }
  const key = `${host}/${camera}/${ymd}/${filename}`;
  if (!_keptClips.has(key)) {
    _keptClips.set(key, resolveUserRole().then(role => {
      if (!ACCOUNT_ROLES.includes(role)) return null;
      return fetch(`/api/keep/${host}/${camera}/${ymd}/${encodeURIComponent(filename)}`,
                   { method: 'POST' })
        .then(r => (r.ok ? r.json() : null));
    }).catch(() => null).then(data => {
      if (!data) _keptClips.delete(key);   // retry on the next play
      return data;
    }));
  }
  return _keptClips.get(key);
}

/** Wire a lock button anywhere a single clip is shown — the shared video modal
 *  calls this so a clip is lockable from every section that opens it (VDB, RMS,
 *  Events, Archive), removing the need to SSH to the station to lock a clip.
 *  Self-gating like wireHofButton: the button hides unless the clip has a full
 *  identity (host+camera+date+filename) AND the session may lock this station.
 *  Locking is one-way from the dashboard (unlock is SSH-only), so an already-
 *  locked clip renders a disabled "Locked" marker; only an unlocked `.mkv` chunk
 *  is clickable. `onLocked(data)` lets the caller sync its own list/card state.
 *  Returns a promise (role resolution may need a fetch) — callers may ignore it.
 *  @param {HTMLElement} btn
 *  @param {object} o {host, camera, date, filename, locked, lockType, onLocked} */
export async function wireLockButton(btn, o) {
  if (!btn) return;
  const { host, camera, date, filename, locked, lockType, onLocked } = o || {};
  btn.style.display = 'none';   // hidden until identity + role check out
  if (!(host && camera && date && filename)) return;
  await resolveUserRole();
  if (!canToggleLock(host)) return;
  btn.onclick = null;
  if (locked) {
    // One-way lock: the dashboard can add a lock but not remove it.
    btn.style.display = '';
    btn.disabled = true;
    btn.innerHTML = LOCK_ICON + ' Locked';
    btn.title = lockType === 'manual'
      ? 'Manually locked — unlock via SSH only'
      : 'Detection locked — unlock via SSH only';
    return;
  }
  // Unlocked. The station lock endpoint only accepts the `.mkv` chunk, so a
  // reconstructed/faststart `.mp4` view has nothing to lock — hide rather than
  // offer an action the server would reject.
  if (!/\.mkv$/i.test(filename)) { btn.style.display = 'none'; return; }
  btn.style.display = '';
  btn.disabled = false;
  btn.innerHTML = UNLOCK_ICON + ' Lock clip';
  btn.title = 'Lock this clip so it is retained permanently';
  btn.onclick = async () => {
    if (btn.disabled) return;
    btn.disabled = true;
    const restore = btn.innerHTML;
    btn.innerHTML = UNLOCK_ICON + ' Locking…';
    try {
      const data = await lockClip(host, camera, date, filename);
      btn.innerHTML = LOCK_ICON + ' Locked';
      btn.title = 'Locked — unlock via SSH only';
      onLocked?.(data);
    } catch (e) {
      alert('Lock failed: ' + e.message);
      btn.innerHTML = restore;
      btn.disabled = false;
    }
  };
}

/** POST a clip-scoped process request (lock if unlocked + re-encode + archive
 *  upload of ONE chunk) and return the station's response. The station endpoint
 *  is exempt from its read-only guard, so this works where the whole-night
 *  dawn run is refused with station_readonly. Throws like lockClip. */
export async function processClip(host, camera, date, filename) {
  const resp = await fetch(
    `/api/process-clip/${host}/${camera}/${date}/${encodeURIComponent(filename)}`,
    { method: 'POST' }
  );
  // 409 = already re-encoded and delivered: the server refuses to process a
  // clip twice, so hand the verdict back instead of throwing.
  if (resp.status === 409) return resp.json();   // { ok: false, processed: true }
  if (!resp.ok && resp.status !== 202) {
    const ct = resp.headers.get('content-type') || '';
    if (ct.includes('text/html') || resp.status === 401) {
      throw new Error(`Session expired (${resp.status}). Please refresh the page.`);
    }
    if (resp.status === 403) throw new Error('You do not have access to this station.');
    const body = await resp.text();
    throw new Error(body.length > 200 ? body.slice(0, 200) + '…' : body);
  }
  return resp.json();   // { ok, locked, lock_type } or { ok, running } on 202
}

// A clip and its BW FF/max-pixel meteor stack share a base name, so the stack
// webp is derivable from ANY detection clip filename, even when a detection
// payload carries no `stack` field. RMS + the station stacker produce
// ``<cam>_<date>_<HHMMSS>_stack.webp`` for every locked (detection) chunk.
// Mirrors the server-side derivation in public_api._derive_stack_name.
const CLIP_STACK_SUFFIXES = ['_color.mkv', '_color.mp4', '.mkv', '.mp4'];

export function deriveStackName(filename) {
  if (!filename) return null;
  for (const s of CLIP_STACK_SUFFIXES) {
    if (filename.endsWith(s)) return filename.slice(0, -s.length) + '_stack.webp';
  }
  return null;
}

/** Map a ``/stack/…`` thumbnail URL to its full-resolution ``/fullstack/…``
 *  twin; any other URL (e.g. the gated ``/api/archive/file/…`` stacks VDB and
 *  highlights pass) is already full-res and comes back unchanged. ``/stack/``
 *  serves the 480 px display thumbnail (480x318 for a 1280x848 stack), which is
 *  right for a grid cell and wrong for anything shown at modal size. */
export function fullResStackUrl(url) {
  if (!url) return url;
  return url.startsWith('/stack/') ? '/fullstack/' + url.slice('/stack/'.length) : url;
}

/** Render the Process button as a disabled "✓ Processed" marker. */
function _markProcessed(btn, title) {
  btn.style.display = '';
  btn.disabled = true;
  btn.textContent = '✓ Processed';
  btn.title = title;
  btn.onclick = null;
}

/** Filenames the station process endpoint accepts — mirror of CHUNK_RE in
 *  station_api.py. A stitched boundary splice (double timestamp, e.g.
 *  MD0001_20260731_185555_185615_color.mkv) is NOT a chunk: it only exists
 *  because the station already re-encoded and delivered it to the archive. */
const _CHUNK_NAME_RE = /^[A-Z0-9]+_\d{8}_\d{6}_color\.mkv$/;
const _SPLICE_NAME_RE = /^[A-Z0-9]+_\d{8}_\d{6}_\d{6}_color\.mkv$/;

/** Wire the clip-scoped Process button in the shared video modal. Self-gating
 *  like wireLockButton: hidden unless the clip has a full identity
 *  (host+camera+date+.mkv filename) AND the session is admin — the server gate
 *  (`@require_station`) also admits the owning host, but the client can't
 *  cheaply check ownership, so non-admin hosts use SSH for this repair action.
 *  The action is clip-scoped on purpose: it delivers THIS chunk to the archive
 *  (lock + re-encode + upload) rather than rerunning the whole night.
 *  A boundary splice, or a clip the payload reports as already re-encoded
 *  (``processed``), renders a disabled "Processed" marker instead: processing
 *  it again would redo delivered work, and the server refuses it (409).
 *  Returns a promise (role resolution may need a fetch) — callers may ignore it.
 *  @param {HTMLElement} btn
 *  @param {object} o {host, camera, date, filename, processed} */
export async function wireProcessClipButton(btn, o) {
  if (!btn) return;
  const { host, camera, date, filename, processed } = o || {};
  btn.style.display = 'none';   // hidden until identity + role check out
  if (!(host && camera && date && filename) || !/\.mkv$/i.test(filename)) return;
  const role = await resolveUserRole();
  if (role !== 'admin') return;
  if (_SPLICE_NAME_RE.test(filename)) {
    _markProcessed(btn, 'Already delivered to the archive — this stitched clip was created during upload');
    return;
  }
  if (!_CHUNK_NAME_RE.test(filename)) return;   // .mkv the station would refuse
  if (processed) {
    _markProcessed(btn, 'Already re-encoded and delivered to the archive');
    return;
  }
  btn.style.display = '';
  btn.disabled = false;
  btn.textContent = 'Process clip';
  btn.title = 'Lock, re-encode and upload this clip to the archive';
  btn.onclick = async () => {
    if (btn.disabled) return;
    btn.disabled = true;
    const restore = 'Process clip';
    btn.textContent = 'Processing…';
    try {
      const data = await processClip(host, camera, date, filename);
      if (data.processed) {
        _markProcessed(btn, 'Already re-encoded and delivered to the archive');
        return;
      }
      btn.textContent = data.running ? '✓ Already running' : '✓ Started';
    } catch (e) {
      const msg = e.message || 'failed';
      btn.textContent = '✗ ' + (msg.length > 40 ? msg.slice(0, 40) + '…' : msg);
    }
    setTimeout(() => { btn.textContent = restore; btn.disabled = false; }, 4000);
  };
}

/* ─────────────────────────────────────────
   fetchJson — guarded fetch wrapper
   ─────────────────────────────────────────
   All internal API calls must go through this helper instead of calling
   fetch(url).then(r => r.json()) directly. The raw pattern silently
   propagates any non-JSON body (login-redirect HTML, 5xx error pages) into
   a SyntaxError, which surfaces as a cryptic "Unexpected token '<'" rather
   than a meaningful error or a redirect to /login.

   Behaviour:
   - 401  → for a logged-in session (state.AUTH_USER set), redirect to /login
            (the session expired). For an ANONYMOUS visitor on a public page
            (no AUTH_USER), a 401 just means "this particular endpoint is
            login-gated" — the visitor was never logged in, so bouncing them
            to /login would eject them from a public page they're allowed to
            see (e.g. /events, whose curated detection feed stays gated). In
            that case we throw the typed error instead and let the caller's
            catch degrade gracefully. Either way a typed error is re-thrown so
            callers in Promise.all / try-catch can detect it.
   - !ok  → throws Error('HTTP <status>') so catch blocks show something useful.
   - non-JSON content-type → throws Error('Not JSON: <content-type>') to catch
            accidental HTML pages that slip through without a 401.
   - ok + JSON → returns parsed response body.

   opts is forwarded verbatim to fetch() so POST/PATCH/etc. work unchanged.

   fetchJsonResponse() applies the identical guards but hands back the
   validated Response instead of the parsed body, for the callers that need a
   response header as well as the payload. fetchJson() is a thin wrapper over
   it, so the 401 / !ok / content-type behaviour can never drift between them.
───────────────────────────────────────── */
export async function fetchJsonResponse(url, opts) {
  const res = await fetch(url, opts);
  if (res.status === 401) {
    // Only a genuine session expiry (we had a user) warrants a login bounce.
    // Anonymous public visitors stay on the page; the caller handles the 401.
    if (state.AUTH_USER) {
      window.location.href = '/login';
    }
    const err = new Error(state.AUTH_USER ? 'Session expired' : 'Unauthorized');
    err.status = 401;
    throw err;
  }
  if (!res.ok) {
    const err = new Error('HTTP ' + res.status);
    err.status = res.status;
    throw err;
  }
  const ct = res.headers.get('content-type') || '';
  if (!ct.includes('application/json') && !ct.includes('text/json')) {
    throw new Error('Not JSON: ' + ct);
  }
  return res;
}

export async function fetchJson(url, opts) {
  const res = await fetchJsonResponse(url, opts);
  return res.json();
}

/* ─────────────────────────────────────────
   Download filename from the actual response format
   ─────────────────────────────────────────
   The download buttons used to blind-rename *.mkv → *.mp4 because the
   station shortclip endpoint re-encodes to MP4. With the archive fallback
   the response can also be a raw Matroska stream, so the saved extension
   must follow the response Content-Type — a video/x-matroska body saved
   as .mp4 confuses players that trust the extension. An unrecognised or
   missing Content-Type keeps the original name (no blind rename). */
const _VIDEO_EXT_BY_TYPE = {
  'video/mp4': '.mp4',
  'video/x-matroska': '.mkv',
  'video/matroska': '.mkv',
  'video/webm': '.webm',
};

export function downloadNameFor(filename, contentType) {
  const name = filename || 'clip';
  const type = (contentType || '').split(';')[0].trim().toLowerCase();
  const ext = _VIDEO_EXT_BY_TYPE[type];
  if (!ext) return name;
  return name.replace(/\.(mkv|mp4|webm)$/i, '') + ext;
}

/**
 * Save `url` as a file. Waits out 202 / 429 / 503 through fetchPaced; once
 * the server answers 200 the probe's body is dropped and the browser's own
 * downloader fetches the URL, streaming it to disk. The file is named from
 * Content-Disposition, else `fallbackName` with the extension of what was
 * sent. Resolves to the final response; the file was saved only when its
 * status is 200. Rejects on a network error.
 *
 * Reading the body into a Blob first (as this used to) needs the whole file
 * in the page; Chrome spills big blobs to its profile disk and fails the read
 * with "Failed to fetch" when that disk is nearly full (2026-10-19: every
 * clip and composite over ~14 MB on a machine with 3 GB free).
 */
export async function saveDownload(url, fallbackName, { onWait } = {}) {
  const ctl = new AbortController();
  const resp = await fetchPaced(url, onWait, { tries: PREPARING_MAX_TRIES, init: { signal: ctl.signal } });
  if (resp.status !== 200) return resp;
  const m = (resp.headers.get('content-disposition') || '').match(/filename="?([^";\n]+)"?/);
  const name = m ? m[1] : downloadNameFor(fallbackName, resp.headers.get('Content-Type'));
  ctl.abort();
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => a.remove(), 1000);
  return resp;
}

/** Save a Blob the page already holds (a frame grab, a rendered image, a
 *  clip read whole) as a file named `name`. URL downloads go through
 *  saveDownload, which never holds the file in the page. */
export function saveBlob(blob, name) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}

/** Toast text for a saveDownload that did not save. */
export function downloadFailedText(status) {
  if (status === 404) return 'Clip not available';
  if (status === 202 || status === 503) return 'The clip is still being prepared, try again in a minute';
  return `Download failed (${status})`;
}

/**
 * Append the modal's trim window to a shortclip base URL as ?ss=&t=.
 *
 * Every "Download clip" button in a VideoModal must honour the operator's
 * in/out handles: the modal returns { start, end, duration } from
 * getTrimState(), and the station/archive shortclip routes cut [ss, ss+t]
 * server-side. Callers that skipped this shipped the whole ~20 s chunk even
 * when the operator had trimmed to a 2 s meteor.
 *
 * @param {string} baseRoute  Route without query string, e.g.
 *   `/api/archive/shortclip/${cam}/${date}/${encodeURIComponent(filename)}`.
 * @param {{start?:number,end?:number,duration?:number}} trim  getTrimState()
 *   snapshot. Missing/degenerate values fall back to a full 0..duration cut.
 * @returns {{url:string, ss:number, t:number, trimmed:boolean}}
 *   `trimmed` is true only when the operator set a real sub-selection (the
 *   window is strictly inside the clip) — callers can use it to decide
 *   whether a full-clip download is acceptable.
 */
export function buildTrimmedShortclipUrl(baseRoute, trim) {
  const duration = Number.isFinite(trim?.duration) && trim.duration > 0
    ? trim.duration
    : null;
  let ss = Number.isFinite(trim?.start) ? Math.max(0, trim.start) : 0;
  let end = Number.isFinite(trim?.end) ? trim.end : (duration ?? 0);
  if (duration != null) end = Math.min(end, duration);
  if (end <= ss) end = duration != null ? duration : ss + 0.5;
  const t = Math.max(0.5, end - ss);
  // A sub-selection means the window is strictly inside the full clip. With no
  // known duration we can only tell from a non-zero start.
  const trimmed = duration != null
    ? (ss > 0.01 || end < duration - 0.01)
    : ss > 0.01;
  const url = `${baseRoute}?ss=${ss.toFixed(2)}&t=${t.toFixed(2)}`;
  return { url, ss, t, trimmed };
}

/* ─────────────────────────────────────────
   Retry a <video> whose clip is still being prepared
   A clip that must come from the station (footage, tonight's clips) is
   pulled in the background, and while that runs the server answers 202
   (or 503 when every remux slot is busy) with Retry-After instead of
   hanging into nginx's 30 s limit. A <video> only sees "error", so on
   error ask the server what it said and reload when it says to wait.
   Returns true when a retry is scheduled, so the caller keeps its loading
   state instead of showing "not available".
───────────────────────────────────────── */
export const PREPARING_MAX_TRIES = 12;

export function retryVideoIfPreparing(video, { onWaiting, onGiveUp } = {}) {
  const src = video.getAttribute('src') || '';
  if (!src || src.startsWith('blob:') || src.startsWith('data:')) return false;
  const tries = video._prepTries || 0;
  if (tries >= PREPARING_MAX_TRIES) return false;
  video._prepTries = tries + 1;
  const reload = () => {
    if (video.getAttribute('src') !== src) return;   // closed or moved on
    video.src = src;
    video.load();
  };
  fetch(src, { method: 'HEAD', cache: 'no-store' }).then(r => {
    if (r.status === 202 || r.status === 503) {
      const after = Math.min(30, Math.max(1,
        parseInt(r.headers.get('Retry-After') || '3', 10) || 3));
      if (onWaiting) onWaiting(after);
      setTimeout(reload, after * 1000);
    } else if (r.ok) {
      reload();          // became ready between the two requests
    } else if (onGiveUp) {
      onGiveUp();
    }
  }).catch(() => { if (onGiveUp) onGiveUp(); });
  return true;
}

/* ─────────────────────────────────────────
   Per-station poll backoff for live-thumbnail polling
   ─────────────────────────────────────────
   The overview's Live View and station band poll
   /api/latest_ff_maxpixel/<host>/<cam> every 15 s for as long as the tab is
   open. A station that is down answers 503 on every one of those passes, and
   the server only reaches that 503 after trying to pull the frame from the
   station itself — so a browser left open overnight turned a handful of
   offline cameras into thousands of pointless upstream round-trips (3,751 x
   503 on /api/latest_ff_maxpixel between 00:00 and 05:00 on 2026-08-25).

   This is a plain exponential backoff keyed by whatever the caller polls
   (host, or host/camera): each consecutive failure pushes the next allowed
   attempt further out — 30 s, 1 min, 5 min, then a 15 min ceiling — and a
   single success clears the key entirely. Online stations never accumulate a
   failure, so their polling cadence is untouched.

   `now` is injectable so the schedule is unit-testable without timers. */
export const POLL_BACKOFF_STEPS_MS = [30000, 60000, 300000, 900000];

export function createPollBackoff(steps = POLL_BACKOFF_STEPS_MS, now = () => Date.now()) {
  const entries = new Map();   // key -> { failures, until }

  /* Delay after `failures` consecutive failures (1-based), capped at the last
     step so a permanently dead station settles at one probe per 15 min rather
     than growing without bound. */
  function delayFor(failures) {
    const i = Math.min(Math.max(failures, 1), steps.length) - 1;
    return steps[i];
  }

  return {
    delayFor,
    /* True while the key is inside its backoff window -> skip this pass. */
    blocked(key) {
      const e = entries.get(key);
      return !!e && now() < e.until;
    },
    /* Record a failed (or deliberately skipped-because-offline) attempt. */
    fail(key) {
      const failures = (entries.get(key)?.failures || 0) + 1;
      const delay = delayFor(failures);
      entries.set(key, { failures, until: now() + delay });
      return delay;
    },
    /* A success means the station is answering again: poll at full rate. */
    succeed(key) {
      entries.delete(key);
    },
    /* Drop a key we no longer poll (card removed, panel closed). */
    forget(key) {
      entries.delete(key);
    },
    failures(key) {
      return entries.get(key)?.failures || 0;
    },
    size() {
      return entries.size;
    },
  };
}

/* ─────────────────────────────────────────
   Colour-segment cadence alarm (shared wording)
   ─────────────────────────────────────────
   Stations report a per-camera segment-health block on /api/status: how many
   colour segments in the trailing hour were cut back-to-back and how many
   holes opened up. When a camera stream corrupts, ffmpeg's segment muxer cuts
   ~1 s clips instead of 20 s ones (RO000Y: 1543 in one hour); when it stalls,
   the night gets holes instead. Neither moves any other health signal, which
   is why both used to surface only the morning after.

   The thresholds and the alarm decision are server side (dashboard/
   segment_health.py) — the frontend only formats, and it does so in one place
   so the fleet table and the station page say the same thing. */
export const SEGMENT_ALARM_TEXT = {
  storm:  'clip storm',
  stalls: 'capture stalls',
  both:   'clip storm + stalls',
};

/* Badge for ONE camera's segment-health block, or null when there is nothing
   to flag — a healthy camera, a daytime idle camera, or a station bundle too
   old to report cadence at all. */
export function segmentHealthBadge(health) {
  if (!health || !health.alarm) return null;
  const parts = [];
  if (health.short) parts.push(`${health.short} back-to-back segments`);
  if (health.stalls) parts.push(`${health.stalls} gaps`);
  const windowLabel = health.window_min ? `last ${health.window_min} min` : 'last hour';
  const detail = parts.length ? ` — ${parts.join(', ')}` : '';
  return {
    label: SEGMENT_ALARM_TEXT[health.alarm] || 'segment fault',
    cls: health.alarm === 'stalls' ? 'stalls' : 'storm',
    title: `Colour capture cadence is broken in the ${windowLabel}${detail}. `
         + 'Check the camera stream now; tonight’s footage is being lost as this runs.',
  };
}

/* ─────────────────────────────────────────
   station_readonly safety net (2026-07 hardening)
   ─────────────────────────────────────────
   The station API is read-only from the network (fail-closed guard in
   station_api.py since 2026-09-26; opt-in via ROVIMEN_STATION_READONLY=1
   before that) and answers a mutating call outside its allowlist with
   403 {"error":"station_readonly"}. Every such control has been
   removed from the UI, but a leftover path (stale tab, cached JS, future
   regression) should tell the operator what happened instead of surfacing a
   bare "HTTP 403". We wrap window.fetch so ANY such response — whether it
   went through fetchJson or a raw fetch — shows the toast. The response is
   cloned before the body peek, so callers still consume it normally. */
(function initReadonlyFetchGuard() {
  const origFetch = window.fetch.bind(window);
  window.fetch = async (...fetchArgs) => {
    const res = await origFetch(...fetchArgs);
    if (res.status === 403) {
      try {
        const body = await res.clone().json();
        if (body && body.error === 'station_readonly') {
          _toast('Read-only station — use SSH', 'error');
        }
      } catch { /* non-JSON 403 — not ours */ }
    }
    return res;
  };
})();

/* ─────────────────────────────────────────
   Client events: what the server cannot see
   ─────────────────────────────────────────
   A request nginx gave up on (504 after its 30 s timeout) is logged by Flask
   as a 200 once it finishes, a <video> that never loads leaves no trace, and
   a tab switch inside a page makes no request. All three are reported to
   /api/client-event (routes/telemetry.py) so the admin Usage Stats can count
   them. Best-effort by construction: paths only (no query, no bodies),
   batched, capped per page load and per endpoint, never throws, never
   reports its own requests, and stops for the page on 401/403/429 (e.g. an
   anonymous visitor, for whom the endpoint is gated). */
const CLIENT_EVENT_URL = '/api/client-event';
const CE_FLUSH_MS = 5000;        // batch window
const CE_BATCH = 20;             // = server MAX_EVENTS_PER_POST
const CE_MAX_PER_PAGE = 60;      // events per page load
const CE_MAX_PER_KEY = 3;        // a 503-ing poll reports 3 times, not forever
const CE_TAB_DWELL_MS = 1000;    // a tab counts once it stayed open this long

/** Same-origin path of `url`, or null (other origins are not ours to report). */
export function ceSamePath(url, origin = window.location.origin) {
  try {
    const u = new URL(url, origin);
    return u.origin === origin ? u.pathname : null;
  } catch {
    return null;
  }
}

/** 'http' | 'timeout' | 'network' for a failure worth reporting, else null.
 *  Aborts are deliberate (tab switch, superseded request) and 4xx are the
 *  server's own considered answer, logged as such. */
export function ceFailureReason(status, err) {
  if (err) {
    if (err.name === 'AbortError') return null;
    return err.name === 'TimeoutError' ? 'timeout' : 'network';
  }
  return status >= 500 ? 'http' : null;
}

/** Batching queue behind the beacon. `send(events)` resolves to the HTTP
 *  status (or rejects); injectable timers keep it unit-testable. */
export function createClientEventQueue(send, {
  flushMs = CE_FLUSH_MS, batch = CE_BATCH, maxPerPage = CE_MAX_PER_PAGE,
  maxPerKey = CE_MAX_PER_KEY, setTimer = setTimeout, clearTimer = clearTimeout,
} = {}) {
  let pending = [];
  let timer = null;
  let accepted = 0;
  let disabled = false;
  const perKey = new Map();
  const flush = () => {
    if (timer) { clearTimer(timer); timer = null; }
    if (disabled || !pending.length) return;
    const chunk = pending.splice(0, batch);
    try {
      Promise.resolve(send(chunk)).then(status => {
        if (status === 401 || status === 403 || status === 429) q.disable();
      }, () => {});
    } catch { /* never let telemetry throw */ }
    if (pending.length) timer = setTimer(flush, flushMs);
  };
  const q = {
    push(ev, key) {
      if (disabled || accepted >= maxPerPage) return false;
      if (key) {
        const n = perKey.get(key) || 0;
        if (n >= maxPerKey) return false;
        perKey.set(key, n + 1);
      }
      accepted += 1;
      pending.push(ev);
      if (!timer) timer = setTimer(flush, flushMs);
      return true;
    },
    flush,
    disable() { disabled = true; pending = []; if (timer) { clearTimer(timer); timer = null; } },
    get accepted() { return accepted; },
  };
  return q;
}

let _ceQueue = null;
let _ceTabTimer = null;
let _ceLastTab = null;

function _ceSend(events) {
  // The pristine fetch, so the beacon never reports (or counts) itself.
  const f = window.__rovimenOrigFetch || window.fetch;
  return f(CLIENT_EVENT_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ events }),
    credentials: 'same-origin',
    keepalive: true,
  }).then(r => r.status);
}

function _ce() {
  if (!_ceQueue) _ceQueue = createClientEventQueue(_ceSend);
  return _ceQueue;
}

/** Report a request or media load the user saw fail. */
export function reportClientFailure(path, status, reason) {
  try {
    if (!path || path === CLIENT_EVENT_URL) return;
    _ce().push(
      { type: 'failure', path, status: status || 0, reason, page: window.location.pathname },
      `${path}|${status || reason}`,
    );
  } catch { /* never let telemetry throw */ }
}

/** Report an in-page tab view (tabs that switch without a request). Counted
 *  once the tab has stayed open CE_TAB_DWELL_MS; re-selecting the tab that
 *  is already showing does not count again. */
export function reportTabView(page, tab) {
  try {
    if (_ceTabTimer) clearTimeout(_ceTabTimer);
    _ceTabTimer = setTimeout(() => {
      _ceTabTimer = null;
      const key = `${page}|${tab}`;
      if (key === _ceLastTab) return;
      _ceLastTab = key;
      _ce().push({ type: 'tab', page, tab });
    }, CE_TAB_DWELL_MS);
  } catch { /* never let telemetry throw */ }
}

(function installClientEvents() {
  if (typeof window === 'undefined' || typeof window.fetch !== 'function') return;
  if (window.__rovimenClientEvents) return;
  // Unit tests (jsdom) stub fetch and fake timers; reporting there would add
  // calls they count. The helpers above stay importable for their own tests.
  if (/\bjsdom\b/i.test((window.navigator && window.navigator.userAgent) || '')) return;
  window.__rovimenClientEvents = true;
  const inner = window.fetch;
  window.__rovimenOrigFetch = inner.bind(window);
  let leaving = false;
  window.addEventListener('pagehide', () => {
    leaving = true;   // fetches cut off by navigation are not failures
    try { _ceQueue && _ceQueue.flush(); } catch { /* ignore */ }
  });
  window.fetch = function (input, init) {
    const p = inner.call(window, input, init);
    try {
      const url = typeof input === 'string' ? input : (input && input.url) || String(input);
      const path = ceSamePath(url);
      if (path && path !== CLIENT_EVENT_URL && p && typeof p.then === 'function') {
        p.then(
          r => { const why = ceFailureReason(r.status); if (why) reportClientFailure(path, r.status, why); },
          e => { if (leaving) return; const why = ceFailureReason(0, e); if (why) reportClientFailure(path, 0, why); },
        );
      }
    } catch { /* the caller's promise is returned untouched either way */ }
    return p;
  };
  // <video>/<source> errors don't bubble; catch them on the way down.
  document.addEventListener('error', e => {
    const t = e.target;
    if (!t || (t.tagName !== 'VIDEO' && t.tagName !== 'SOURCE')) return;
    if (t.error && t.error.code === 1) return;   // MEDIA_ERR_ABORTED: user/page stopped it
    const src = t.currentSrc || t.src || '';
    if (!src || src.startsWith('blob:') || src === window.location.href) return;  // src cleared on close
    const path = ceSamePath(src);
    if (path && path !== window.location.pathname) reportClientFailure(path, 0, 'media');
  }, true);
})();

// Expose to global scope for inline onclick handlers
window.openAppPanel = openAppPanel;
window.appPanelClose = appPanelClose;
window.fetchJson = fetchJson;

// ── Header "Go to station" (every page) ─────────────────────────────────────
// One click opens it, typing filters by station ID, place or camera code,
// Enter (or a click) opens the station. "/" anywhere opens it with focus in the
// filter. The list comes from /api/stations, which already applies the
// per-user visibility rules, and is fetched once on first open.
(function initStationJump() {
  const start = () => {
    const root = document.getElementById('nav-station-jump');
    if (!root) return;
    const btn = root.querySelector('.nav-station-btn');
    const menu = root.querySelector('.nav-station-menu');
    const input = root.querySelector('.nav-station-filter');
    const list = root.querySelector('.nav-station-list');
    let stations = null;
    let active = 0;

    const render = () => {
      const q = input.value.trim().toLowerCase();
      const rows = (stations || []).filter(s => !q || s.hay.includes(q));
      active = Math.min(active, Math.max(rows.length - 1, 0));
      list.innerHTML = rows.length
        ? rows.map((s, i) => `<li role="option" aria-selected="${i === active}"><a href="/station/${encodeURIComponent(s.key)}"${i === active ? ' class="is-active"' : ''}><span class="ns-key" translate="no">${escHtml(s.key)}</span><span>${escHtml(s.label)}</span><span class="ns-cams" translate="no">${escHtml(s.cams)}</span></a></li>`).join('')
        : `<li class="ns-empty">${stations ? 'No station matches.' : 'Loading…'}</li>`;
    };
    const load = async () => {
      if (stations) return;
      try {
        const data = await (await fetch('/api/stations')).json();
        stations = Object.entries(data).map(([key, info]) => {
          const codes = (info.cameras || []).map(c => c.code).filter(Boolean);
          const label = info.label || '';
          return { key, label, cams: codes.join(' '), hay: `${key} ${label} ${codes.join(' ')}`.toLowerCase() };
        }).sort((a, b) => a.key.localeCompare(b.key, 'en', { numeric: true }));
      } catch {
        stations = [];
      }
      render();
    };
    const open = () => {
      menu.hidden = false;
      btn.setAttribute('aria-expanded', 'true');
      input.value = '';
      active = 0;
      render();
      load();
      input.focus();
    };
    const close = (refocus) => {
      if (menu.hidden) return;
      menu.hidden = true;
      btn.setAttribute('aria-expanded', 'false');
      if (refocus) btn.focus();
    };

    btn.addEventListener('click', () => (menu.hidden ? open() : close(true)));
    input.addEventListener('input', () => { active = 0; render(); });
    input.addEventListener('keydown', (e) => {
      const links = list.querySelectorAll('a');
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (!links.length) return;
        active = (active + (e.key === 'ArrowDown' ? 1 : -1) + links.length) % links.length;
        render();
        list.querySelector('a.is-active')?.scrollIntoView({ block: 'nearest' });
      } else if (e.key === 'Enter') {
        e.preventDefault();
        const target = list.querySelector('a.is-active') || links[0];
        if (target) window.location.href = target.href;
      } else if (e.key === 'Escape') {
        e.preventDefault();
        close(true);
      }
    });
    document.addEventListener('click', (e) => { if (!root.contains(e.target)) close(false); });
    document.addEventListener('keydown', (e) => {
      if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target;
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
      e.preventDefault();
      open();
    });
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();

/** The window every meteor player opens on: the meteor starts CLIP_PRE_S
 *  into a CLIP_PRE_S + CLIP_POST_S (10 s) window, leaving room for a
 *  persistent train. Offsets are the station's measured meteor start in the
 *  clip (clip_timing.py), so the meteor really is at +2 s. */
export const CLIP_PRE_S = 2;
export const CLIP_POST_S = 8;

/* ─────────────────────────────────────────
   Toggle buttons (map layers, coverage, altitude, plot type) carry their
   state both as the .active class (styling) and aria-pressed (assistive tech).
───────────────────────────────────────── */
export function setPressed(btn, on) {
  if (!btn) return;
  btn.classList.toggle('active', !!on);
  btn.setAttribute('aria-pressed', on ? 'true' : 'false');
}

/* ─────────────────────────────────────────
   Overview top bar (overview.html, both "/" and "/network"): the station and
   camera counts in #ov-summary and the network numbers in #ov-net-stats.
   Here because both page modules draw them (dashboard-home.js on "/",
   dashboard-overview.js on "/network") and neither loads the other.
───────────────────────────────────────── */
export function renderOverviewSummary(data) {
  const entries=Object.entries(data);
  const comm=entries.filter(([,d])=>d.status==='commissioning').length;
  const active=entries.filter(([,d])=>d.status!=='commissioning');
  const onlineSt=active.filter(([,d])=>d.online).length;
  const offlineSt=active.length-onlineSt;
  const onlineCams=active.filter(([,d])=>d.online).reduce((s,[,d])=>s+(d.cameras?.length||0),0);
  const offlineCams=active.filter(([,d])=>!d.online).reduce((s,[,d])=>s+(d.cameras?.length||0),0);
  const commCams=entries.filter(([,d])=>d.status==='commissioning').reduce((s,[,d])=>s+(d.cameras?.length||0),0);
  const summary=document.getElementById('ov-summary');
  summary.innerHTML =
    '<div class="ov-summary-hdr"></div>' +
    '<div class="ov-summary-hdr">Online</div>' +
    '<div class="ov-summary-hdr">Comm.</div>' +
    '<div class="ov-summary-hdr">Offline</div>' +
    '<div class="ov-summary-label">Stations</div>' +
    '<div class="ov-summary-cell green">' + onlineSt + '</div>' +
    '<div class="ov-summary-cell orange">' + comm + '</div>' +
    '<div class="ov-summary-cell red">' + offlineSt + '</div>' +
    '<div class="ov-summary-label">Cameras</div>' +
    '<div class="ov-summary-cell green">' + onlineCams + '</div>' +
    '<div class="ov-summary-cell orange">' + commCams + '</div>' +
    '<div class="ov-summary-cell red">' + offlineCams + '</div>';
}

export async function loadOverviewNetStats() {
  const el = document.getElementById('ov-net-stats');
  if (!el) return;
  try {
    const r = await fetch('/api/network-stats');
    if (!r.ok) return;
    const d = await r.json();
    const s = d.network_summary || {};
    const dbl = d.double_station_coverage_km2 || {};
    const fmt = v => v != null ? Math.round(v).toLocaleString() : '—';
    el.style.display = '';
    let html = `
      <div class="ns-item"><span class="ns-val blue">${fmt(d.total_covered_area_km2)}</span><span class="ns-lbl">km&sup2; coverage (100 km)</span></div>
      <div class="ns-item"><span class="ns-val green">${fmt(dbl['100km'])}</span><span class="ns-lbl">km&sup2; double-station</span></div>
      <div class="ns-item"><span class="ns-val">${fmt(d.atmospheric_volume_km3)}</span><span class="ns-lbl">km&sup3; volume (25–130 km)</span></div>
      <div class="ns-item"><span class="ns-val yellow">${fmt(d.expected_meteoric_flux_per_hour)}/hr</span><span class="ns-lbl">sporadic flux (&gt;+4.5 mag)</span></div>`;
    const oc = d.orbit_counts;
    if (oc) {
      html += `
        <div class="ns-item"><span class="ns-val blue">${fmt(oc.last_12_months)}</span><span class="ns-lbl">ROVIMEN orbits (12 mo)</span></div>
        <div class="ns-item"><span class="ns-val">${fmt(oc.last_30_days)}</span><span class="ns-lbl">orbits (30 d)</span></div>
        <div class="ns-item"><span class="ns-val green">${fmt(oc.last_night)}</span><span class="ns-lbl">orbits ${oc.last_night_date ? '(' + oc.last_night_date + ')' : '(last night)'}</span></div>
        <div class="ns-item"></div>`;
    }
    el.innerHTML = html;
  } catch(e) {}
}
