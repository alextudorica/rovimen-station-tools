import { escHtml, fmtDate, _modalOpen, _modalClose } from './dashboard-common.js';
/*
 * dashboard-spectro.js
 * ────────────────────
 * Spectro tab — grating-camera meteor spectroscopy for stations that carry a
 * `spectro: true` camera (today only gmnro01's GH662S/GH662W pair).
 *
 * One night at a time, because that is the unit the station's dawn pipeline
 * produces and the unit an operator reviews. Each event is a card with the
 * pictures that exist for it: the widefield frame that fired the trigger, the
 * spectro-camera frame with the projected track drawn on it, and the reduced
 * spectrum when one was extracted. Click any of them to enlarge.
 *
 * Depends on dashboard-common.js (escHtml, _modalOpen, _modalClose) and on
 * window.canAccessTab / window._accessBanner from dashboard-station.js.
 */

const STATE = {
  host: null,
  data: null,          // /api/spectro payload
  night: null,         // 'YYYYMMDD'
  cam: null,           // spectro camera code
  filter: 'all',       // all | detected | spectrum
  loading: false,
};

/* Lightbox: a flat list of every image on the rendered night, so the arrows
   walk the whole night rather than one card. */
let _lightbox = { items: [], i: 0 };

const STATE_LABEL = {
  spectrum: 'spectrum extracted',
  transient: 'transient, no spectrum',
  none: 'nothing on track',
};

/* Every value on this tab comes from a station-authored index.json (uploaded
   over an authenticated ingest key, but still not the dashboard's own text),
   so it is escaped or coerced before it reaches innerHTML. */
function _num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

function _fileUrl(cam, date, id, name) {
  return `/api/archive/file/${encodeURIComponent(cam)}/${encodeURIComponent(date)}` +
         `/spectro/${encodeURIComponent(id)}__${encodeURIComponent(name)}`;
}

/* The three pictures an event can have, in reading order: what triggered it,
   what the grating camera saw, what came out of the reduction. */
function _imagesFor(cam, date, ev) {
  const files = ev.files || [];
  const out = [];
  if (files.includes('wide.jpg')) {
    out.push({ kind: 'wide', label: 'Widefield trigger frame',
               url: _fileUrl(cam, date, ev.id, 'wide.jpg') });
  }
  if (files.includes('spectro.jpg')) {
    out.push({ kind: 'spectro', label: 'Grating camera, track overlaid',
               url: _fileUrl(cam, date, ev.id, 'spectro.jpg') });
  }
  if (files.includes('spectrum.png')) {
    out.push({ kind: 'spectrum', label: 'Reduced spectrum',
               url: _fileUrl(cam, date, ev.id, 'spectrum.png') });
  }
  return out;
}

/* A storagebox read that gets shed answers 503, and an <img> that took one
   stays broken until the whole page is reloaded — on a 170-picture night that
   left a third of the gallery empty. Retry once, after a pause long enough for
   the burst that caused the shed to have drained. Once only, and marked on the
   element, so a genuinely missing file does not become a request loop; the
   query parameter is what gets the browser to re-request rather than re-use
   the failed response, and the route ignores it. */
const _IMG_RETRY_DELAY_MS = 2500;

function spectroImgRetry(img) {
  if (!img || img.dataset.spectroRetried) return;
  img.dataset.spectroRetried = '1';
  const url = img.getAttribute('src');
  if (!url) return;
  setTimeout(() => {
    img.src = `${url}${url.includes('?') ? '&' : '?'}retry=1`;
  }, _IMG_RETRY_DELAY_MS);
}

function _matchesFilter(ev) {
  if (STATE.filter === 'spectrum') return ev.state === 'spectrum';
  if (STATE.filter === 'detected') return ev.state === 'spectrum' || ev.state === 'transient';
  return true;
}

/* ── rendering ─────────────────────────────────────────────────────────── */

function mkNightPicker(cam, nights) {
  if (!nights.length) return '';
  const opts = nights.map(n => {
    const c = n.counts || {};
    const sel = n.date === STATE.night ? ' selected' : '';
    const ev = _num(c.events), sp = _num(c.spectra);
    return `<option value="${escHtml(n.date)}"${sel}>${escHtml(fmtDate(n.date))} — ` +
           `${ev} event${ev === 1 ? '' : 's'}` +
           `${sp ? `, ${sp} spectra` : ''}</option>`;
  }).join('');
  return `<select id="spectro-night" aria-label="Night" onchange="spectroOnNightChange()">${opts}</select>`;
}

function mkSummary(night) {
  const c = (night && night.counts) || {};
  const chip = (n, label, cls = '') =>
    `<span class="spectro-chip ${cls}"><b>${_num(n)}</b> ${escHtml(label)}</span>`;
  return `<div class="spectro-summary">
    ${chip(c.events, 'events')}
    ${chip(c.rms, 'from RMS detections')}
    ${chip(c.widefield, 'from widefield streaks')}
    ${chip(c.detected, 'with a transient', _num(c.detected) ? 'ok' : '')}
    ${chip(c.spectra, 'with a spectrum', _num(c.spectra) ? 'good' : '')}
  </div>`;
}

function mkFilterBar() {
  const btn = (key, label) =>
    `<button type="button" class="spectro-filter${STATE.filter === key ? ' active' : ''}"
             aria-pressed="${STATE.filter === key}" onclick="spectroSetFilter('${key}')">${escHtml(label)}</button>`;
  return `<div class="spectro-filters">
    ${btn('all', 'All events')}
    ${btn('detected', 'With a transient')}
    ${btn('spectrum', 'With a spectrum')}
  </div>`;
}

function mkEventCard(cam, date, ev, startIdx) {
  const imgs = _imagesFor(cam, date, ev);
  const thumbs = imgs.length
    ? imgs.map((im, k) => `
        <figure class="spectro-thumb">
          <img src="${im.url}" alt="${escHtml(im.label)}" loading="lazy" decoding="async"
               onerror="spectroImgRetry(this)"
               role="button" tabindex="0" aria-label="Open ${escHtml(im.label)}"
               onclick="spectroLightbox(${startIdx + k})">
          <figcaption>${escHtml(im.label)}</figcaption>
        </figure>`).join('')
    : `<div class="spectro-noimg">No preview for this event — it was cut before
        the pipeline started writing them.</div>`;

  const links = [];
  if ((ev.files || []).includes('spectrum.csv')) {
    links.push(`<a href="${_fileUrl(cam, date, ev.id, 'spectrum.csv')}" download>CSV</a>`);
  }
  if ((ev.files || []).includes('reduction.json')) {
    links.push(`<a href="${_fileUrl(cam, date, ev.id, 'reduction.json')}"
                   target="_blank" rel="noopener">reduction</a>`);
  }
  if ((ev.files || []).includes('event.json')) {
    links.push(`<a href="${_fileUrl(cam, date, ev.id, 'event.json')}"
                   target="_blank" rel="noopener">event</a>`);
  }

  const st = ['spectrum', 'transient', 'none'].includes(ev.state) ? ev.state : 'none';
  const trig = ev.trigger === 'widefield' ? 'widefield' : 'RMS';
  const src = ev.source ? ` · ${escHtml(ev.source)}` : '';
  const score = (ev.score || ev.score === 0) ? ` · score ${escHtml(String(ev.score))}` : '';

  return `<article class="spectro-card spectro-state-${escHtml(st)}">
    <header class="spectro-card-hdr">
      <span class="spectro-time">${escHtml(ev.time || '')}</span>
      <span class="spectro-badge spectro-trigger-${ev.trigger === 'widefield' ? 'wide' : 'rms'}">${trig}${src}</span>
      <span class="spectro-badge spectro-st-${escHtml(st)}">${escHtml(STATE_LABEL[st] || st)}</span>
      <span class="spectro-id">${escHtml(ev.id)}${score}</span>
    </header>
    <div class="spectro-thumbs">${thumbs}</div>
    ${links.length ? `<footer class="spectro-links">${links.join(' · ')}</footer>` : ''}
  </article>`;
}

export function renderSpectro(host) {
  const el = document.getElementById('pane-spectro');
  if (!el) return;
  // canAccessTab/_accessBanner live in dashboard-station.js, which is a module
  // like this one: reach them through window, the way dashboard-vdb.js does.
  if (window.canAccessTab && !window.canAccessTab('spectro', host)) {
    el.innerHTML = window._accessBanner('Spectroscopy');
    return;
  }
  if (STATE.host !== host) {
    STATE.host = host; STATE.data = null; STATE.night = null; STATE.cam = null;
  }
  if (!STATE.data) {
    if (!STATE.loading) spectroLoad(host);
    el.innerHTML = `<div class="card"><div class="card-title">Spectroscopy</div>
      <div class="loading">Loading spectroscopy products…</div></div>`;
    return;
  }

  const j = STATE.data;
  if (!j.spectro) {
    el.innerHTML = `<div class="card"><div class="card-title">Spectroscopy</div>
      <div class="no-data">This station has no grating camera.</div></div>`;
    return;
  }
  if (j.failed) {
    el.innerHTML = `<div class="card"><div class="card-title">Spectroscopy</div>
      <div class="no-data">Could not list the spectroscopy archive.
        <a href="#" onclick="event.preventDefault();spectroReload()">Retry</a></div></div>`;
    return;
  }

  const cams = j.cameras || [];
  const cam = cams.find(c => c.cam === STATE.cam) || cams.find(c => (c.nights || []).length) || cams[0];
  if (!cam) {
    el.innerHTML = `<div class="card"><div class="card-title">Spectroscopy</div>
      <div class="no-data">No spectroscopy products yet.</div></div>`;
    return;
  }
  STATE.cam = cam.cam;
  const nights = cam.nights || [];
  if (!nights.length) {
    el.innerHTML = `<div class="card"><div class="card-title">Spectroscopy — ${escHtml(cam.cam)}</div>
      <div class="no-data">No events yet. The pipeline runs after dawn; events appear
        here once a detected meteor crosses the grating camera's field.</div></div>`;
    return;
  }
  if (!nights.some(n => n.date === STATE.night)) STATE.night = nights[0].date;
  const night = nights.find(n => n.date === STATE.night);

  const shown = (night.events || []).filter(_matchesFilter);
  // Flat image list for the lightbox, built in the same order as the cards.
  _lightbox.items = [];
  const cards = shown.map(ev => {
    const start = _lightbox.items.length;
    for (const im of _imagesFor(cam.cam, night.date, ev)) {
      _lightbox.items.push({ ...im, id: ev.id, time: ev.time || '' });
    }
    return mkEventCard(cam.cam, night.date, ev, start);
  }).join('');

  el.innerHTML = `
    <div class="card spectro-header-card">
      <div class="spectro-hdr-row">
        <div class="card-title" style="margin:0">Spectroscopy
          <span class="spectro-sub">${escHtml(cam.cam)} · grating camera, reduced after dawn</span>
        </div>
        ${mkNightPicker(cam.cam, nights)}
      </div>
      ${mkSummary(night)}
      ${j.partial ? `<div class="spectro-warn">Some nights could not be read and are missing from the list.
        <a href="#" onclick="event.preventDefault();spectroReload()">Retry</a></div>` : ''}
      ${mkFilterBar()}
    </div>
    ${shown.length
      ? `<div class="spectro-grid">${cards}</div>`
      : `<div class="card"><div class="no-data">No events match this filter on
           ${escHtml(fmtDate(night.date))}.</div></div>`}`;
}

/* ── data ──────────────────────────────────────────────────────────────── */

export const SPECTRO_RETRY_MS = 3000;

export async function spectroLoad(host) {
  if (STATE.loading) return;
  STATE.loading = true;
  // One automatic retry: a gateway timeout usually means the server was still
  // listing the archive behind other storage-box traffic. It finishes and caches
  // the listing anyway, so the second request is typically a cache hit.
  let payload = null;
  for (let attempt = 0; attempt < 2 && payload === null; attempt++) {
    if (attempt) await new Promise(res => setTimeout(res, SPECTRO_RETRY_MS));
    try {
      const r = await fetch(`/api/spectro/${encodeURIComponent(host)}`);
      if (r.ok) payload = await r.json();
    } catch (e) { /* network error: retry, then report */ }
  }
  if (payload === null) payload = { spectro: true, failed: true, cameras: [] };
  STATE.loading = false;
  if (STATE.host !== host) return;   // station switched while in flight
  STATE.data = payload;
  renderSpectro(host);
}

function spectroReload() {
  STATE.data = null;
  renderSpectro(STATE.host);
}

function spectroOnNightChange() {
  const sel = document.getElementById('spectro-night');
  if (!sel) return;
  STATE.night = sel.value;
  renderSpectro(STATE.host);
}

function spectroSetFilter(key) {
  STATE.filter = key;
  renderSpectro(STATE.host);
}

/* ── lightbox ──────────────────────────────────────────────────────────── */

function spectroLightbox(i) {
  const modal = document.getElementById('spectro-modal');
  if (!modal || !_lightbox.items.length) return;
  _lightbox.i = Math.max(0, Math.min(_lightbox.items.length - 1, i));
  spectroLightboxPaint();
  _modalOpen(modal);
}

function spectroLightboxPaint() {
  const it = _lightbox.items[_lightbox.i];
  if (!it) return;
  const img = document.getElementById('spectro-modal-img');
  const title = document.getElementById('spectro-modal-title');
  const dl = document.getElementById('spectro-modal-dl');
  const pos = document.getElementById('spectro-modal-pos');
  img.src = it.url;
  img.alt = it.label;
  title.textContent = `${it.id}${it.time ? ' · ' + it.time : ''} — ${it.label}`;
  dl.href = it.url;
  pos.textContent = `${_lightbox.i + 1} / ${_lightbox.items.length}`;
}

function spectroLightboxStep(delta) {
  if (!_lightbox.items.length) return;
  const n = _lightbox.items.length;
  _lightbox.i = (_lightbox.i + delta + n) % n;
  spectroLightboxPaint();
}

function spectroLightboxClose() {
  const modal = document.getElementById('spectro-modal');
  _modalClose(modal);
  const img = document.getElementById('spectro-modal-img');
  if (img) img.src = '';
}

/* Arrow keys only while the lightbox is open; Escape is handled by the shared
   dispatcher in dashboard-common.js like every other modal. */
document.addEventListener('keydown', e => {
  const modal = document.getElementById('spectro-modal');
  if (!modal || !modal.classList.contains('open')) return;
  if (e.key === 'ArrowRight') { e.preventDefault(); spectroLightboxStep(1); }
  if (e.key === 'ArrowLeft')  { e.preventDefault(); spectroLightboxStep(-1); }
});

/* Exported for the unit tests: the render path is pure enough to assert on
   directly, which is where the filter/lightbox indexing bugs would show. */
export { _imagesFor, _matchesFilter, _num, mkEventCard, mkSummary,
         spectroImgRetry, STATE as _spectroState, _lightbox };

window.renderSpectro = renderSpectro;
window.spectroLoad = spectroLoad;
window.spectroReload = spectroReload;
window.spectroOnNightChange = spectroOnNightChange;
window.spectroSetFilter = spectroSetFilter;
window.spectroLightbox = spectroLightbox;
window.spectroImgRetry = spectroImgRetry;
window.spectroLightboxStep = spectroLightboxStep;
window.spectroLightboxClose = spectroLightboxClose;
