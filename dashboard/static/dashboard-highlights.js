import { state, escHtml, showerFullName, fetchAuthStatus, _toast, viewQuery, fetchPaced, saveBlob, rmsNightOf, CLIP_PRE_S, CLIP_POST_S } from './dashboard-common.js';
import { withHidden, showingHidden, getShowHidden, toggleHtml, wireToggle, hideButtonHtml, hiddenNoteHtml, hideTarget, toggleHide, applyHidden } from './dashboard-hidden.js';
import { VideoModal } from './dashboard-video-modal.js';
import { MultiDetModal } from './dashboard-multi-det-modal.js';
import { mountHofGallery } from './dashboard-hof.js';

// The Highlights page is event-centric: an event is one meteor plus every
// ROVIMEN camera that recorded it (routes/highlights.py, highlight_events.py).
// Each card opens a sheet with all of its stacks and clips, synchronized
// playback of every camera, and the fireball research page when one covers it.

// ── Helpers ───────────────────────────────────────────────────────────────

function el(id) { return document.getElementById(id); }

function fmtMag(v) {
    if (v == null) return '—';
    const n = parseFloat(v);
    return (n > 0 ? '+' : '') + n.toFixed(1);
}

function fmtUtc(iso) {
    if (!iso) return '—';
    const s = String(iso).replace(' ', 'T');
    const d = new Date(s.endsWith('Z') ? s : s + 'Z');
    if (isNaN(d)) return String(iso);
    return d.toISOString().slice(0, 19).replace('T', ' ') + ' UTC';
}

function fmtNum(v, digits, unit) {
    if (v == null || isNaN(v)) return null;
    return parseFloat(v).toFixed(digits) + (unit ? ' ' + unit : '');
}

/** Highlights dates are ISO (YYYY-MM-DD); archive folders are YYYYMMDD. */
export function hlYmd(date) {
    return String(date || '').replace(/-/g, '');
}

/** The camera's Video DB on the night. */
export function hlCameraUrl(w) {
    return `/station/${encodeURIComponent(w.host_key)}/videodb` +
        viewQuery({ cam: w.camera, date: hlYmd(w.date) || null });
}

/** Distinct place names of an event's witnesses, in tile order. */
function places(witnesses) {
    const out = [];
    for (const w of witnesses || []) {
        const p = w.station_label || w.camera;
        if (p && !out.includes(p)) out.push(p);
    }
    return out;
}

/** The camera that saw it brightest and has a stack: a card's one image. */
export function hlBestStack(witnesses) {
    const ws = (witnesses || []).filter(w => w.stack_url);
    const mag = w => (w.apparent_magnitude == null ? Infinity : +w.apparent_magnitude);
    return ws.reduce((best, w) => (best && mag(best) <= mag(w) ? best : w), null);
}

/** Brightness tier of a magnitude, for the headline number's colour. */
export function hlMagTone(m) {
    if (m == null || isNaN(m)) return '';
    return m <= -8 ? 'mag-superbolide' : m <= -4 ? 'mag-fireball' : m <= -2 ? 'mag-bright' : '';
}

/** "12 Oct, 20:10 UTC" */
function fmtWhen(iso) {
    if (!iso) return '—';
    const s = String(iso).replace(' ', 'T');
    const d = new Date(s.endsWith('Z') ? s : s + 'Z');
    if (isNaN(d)) return String(iso);
    return `${d.getUTCDate()} ${d.toLocaleString('en', { month: 'short', timeZone: 'UTC' })}, ${d.toISOString().slice(11, 16)} UTC`;
}

/** "Ghirdoveni + 5 more" (the sheet lists them all). */
function placesShort(pl) {
    return pl.length > 1 ? `${pl[0]} + ${pl.length - 1} more` : (pl[0] || '');
}

// ── State ─────────────────────────────────────────────────────────────────

let _events  = [];      // brightest events of the loaded range
let _reports = [];      // fireball research reports
let _filter  = 'all';
let _isAdmin = false;
let _sheet   = null;    // { kind: 'event'|'report', item }

const _videoModal = new VideoModal(el('hl-modal-container'), { trim: true, nav: true });
const _multiModal = new MultiDetModal(el('hl-mdm-container'));
window._VideoModalCloseAll = () => { VideoModal.closeAll(); MultiDetModal.closeAll(); };

// ── Playback ──────────────────────────────────────────────────────────────

/** MultiDetModal tiles: one per camera, the ones with a clip first. */
function multiVideos(witnesses) {
    return (witnesses || []).map(w => {
        const base = {
            cam: w.camera, station: w.host_key || '', date: hlYmd(w.date),
            label: w.station_label || w.camera, stackUrl: w.stack_url || null,
            hide: { target: hideTarget(w), hidden: !!w.hidden },
        };
        if (!w.clip_url) {
            return { ...base, url: null, offset: 0, filename: null,
                     unavailable: { label: 'No clip', title: 'This camera\'s clip is not in the archive' } };
        }
        return {
            ...base,
            url:         w.clip_url,
            offset:      w.detection_offset_s || 0,
            filename:    w.filename,
            downloadUrl: `${w.clip_url}?download=1`,
        };
    });
}

function playAll(item, title) {
    const videos = multiVideos(item.witnesses);
    if (!videos.some(v => v.url)) { _toast('No clip of this event is in the archive yet', 'error'); return; }
    _multiModal.open({ title, videos, windowPreSec: CLIP_PRE_S, windowPostSec: CLIP_POST_S,
        onHiddenChange: (tile, resp) => {
            const w = item.witnesses.find(x => x.camera === tile.cam);
            if (w) hlOnHiddenChange(item, w, resp);
        } });
}

function playOne(witnesses, idx, title) {
    const playable = witnesses.filter(w => w.clip_url);
    const w = witnesses[idx];
    const pos = playable.indexOf(w);
    if (pos < 0) return;
    const off = w.detection_offset_s ?? null;
    _videoModal.open({
        src:       w.clip_url,
        title:     [title, w.station_label || '', w.camera].filter(Boolean).join(' · '),
        detOffset: off,
        trimStart: off != null ? Math.max(0, off - CLIP_PRE_S) : 0,
        trimEnd:   off != null ? off + CLIP_POST_S : undefined,
        station:   w.host_key || '',
        camera:    w.camera,
        date:      w.date,
        filename:  w.filename,
        loopDlPath: `/loop_clip_archive/${w.camera}/${hlYmd(w.date)}/${encodeURIComponent(w.filename)}`,
        download:  { originalUrl: `${w.clip_url}?download=1`, onClick: btn => _trimDownload(btn) },
        stack:     w.stack_url ? { url: w.stack_url } : null,
        detection: {
            mag_apparent: w.apparent_magnitude,
            shower:       w.shower || 'SPO',
            duration_s:   w.duration_s,
            time_utc:     w.time_utc,
            station:      w.station_label,
            camera:       w.camera,
        },
        nav: {
            hasPrev: pos > 0,
            hasNext: pos < playable.length - 1,
            onPrev:  () => playOne(witnesses, witnesses.indexOf(playable[pos - 1]), title),
            onNext:  () => playOne(witnesses, witnesses.indexOf(playable[pos + 1]), title),
        },
        hide: {
            target: hideTarget(w), hidden: !!w.hidden, page: 'highlights',
            onChange: resp => { if (_sheet) hlOnHiddenChange(_sheet.item, w, resp); },
        },
    });
}

// ── Hidden detections (false positives a user hid; dashboard-hidden.js) ──────

/** A camera of event `item` was hidden or unhidden: mark it; without
 *  "Show hidden" a hidden camera leaves the event, and an event whose
 *  cameras are all hidden leaves the list (the server rule). */
function hlOnHiddenChange(item, w, resp) {
    if (!item || !w) return;
    applyHidden(w, resp);
    const show = showingHidden();
    if (!show && w.hidden) {
        item._hiddenWitnesses = [...(item._hiddenWitnesses || []), w];
        item.witnesses = item.witnesses.filter(x => x !== w);
    } else if (!w.hidden && item._hiddenWitnesses?.includes(w)) {
        // Undo: the camera comes back.
        item._hiddenWitnesses = item._hiddenWitnesses.filter(x => x !== w);
        item.witnesses = [...item.witnesses, w];
    }
    item.hidden = item.witnesses.length > 0 && item.witnesses.every(x => x.hidden);
    const gone = !item.witnesses.length;
    if (_sheet && _sheet.item === item) {
        if (gone) closeSheet();
        else openSheet(_sheet.kind, item, { push: false });
    }
    renderEvents();
}

async function _trimDownload(btn) {
    const ts = _videoModal.getTrimState();
    if (!ts) return;
    const dlBtn = btn || _videoModal._el?.querySelector('.vm-download-btn');
    const label = dlBtn ? dlBtn.innerHTML : null;
    if (dlBtn) { dlBtn.disabled = true; dlBtn.textContent = 'Preparing…'; }
    try {
        const { camera, date, filename, start, end } = ts;
        const url = `/api/highlights/shortclip/${camera}/${date}/${encodeURIComponent(filename)}?ss=${start.toFixed(2)}&t=${(end - start).toFixed(2)}`;
        const resp = await fetchPaced(url, s => { if (dlBtn) dlBtn.textContent = `Busy, retrying in ${s} s…`; });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const cd = resp.headers.get('content-disposition') || '';
        const m  = cd.match(/filename="?([^";\n]+)"?/);
        saveBlob(await resp.blob(), m ? m[1] : 'clip.mp4');
    } catch (e) {
        _toast('Download failed: ' + e.message, 'error');
    } finally {
        if (dlBtn) { dlBtn.disabled = false; dlBtn.innerHTML = label; }
    }
}

async function downloadStacks(witnesses, btn) {
    const stacks = witnesses.filter(w => w.stack_url);
    btn.disabled = true;
    for (let i = 0; i < stacks.length; i++) {
        btn.textContent = `Stack ${i + 1}/${stacks.length}…`;
        const a = document.createElement('a');
        a.href = stacks[i].stack_url;
        a.download = stacks[i].stack_url.split('/').pop();
        document.body.appendChild(a);
        a.click();
        a.remove();
        await new Promise(r => setTimeout(r, 400));
    }
    btn.disabled = false;
    btn.textContent = 'Download all stacks';
}

// ── Report witnesses ──────────────────────────────────────────────────────
//
// A report lists the clips its authors used; the night's detections can hold
// more cameras of the same event. The first time a report is opened or played,
// its night is fetched and the matching event's other cameras are added.

const _fullReports = new Map();   // event_id -> Promise

/** Archive night (YYYY-MM-DD) of a UTC time: before noon belongs to the previous evening. */
export function hlNightOf(timeUtc) {
    const d = new Date(String(timeUtc).replace(' ', 'T') + (String(timeUtc).endsWith('Z') ? '' : 'Z'));
    return rmsNightOf(d).replace(/(\d{4})(\d\d)(\d\d)/, '$1-$2-$3');
}

function completeReport(r) {
    if (!_fullReports.has(r.event_id)) {
        const night = hlNightOf(r.time_utc);
        _fullReports.set(r.event_id, fetchChunk(night, night).then(data => {
            const ev = (data.events || []).find(e => e.report && e.report.url === r.url);
            if (!ev) return false;
            const have = new Set(r.witnesses.map(w => w.camera));
            const extra = ev.witnesses.filter(w => !have.has(w.camera));
            r.witnesses.push(...extra);
            return extra.length > 0;
        }).catch(() => false));
    }
    return _fullReports.get(r.event_id);
}

// ── Event sheet ───────────────────────────────────────────────────────────

function eventTitle(ev) {
    const sh = showerFullName(ev.shower || 'SPO');
    return ev.fireball ? `${sh} fireball` : `${sh} meteor`;
}

function eventFacts(ev) {
    const facts = [
        [ev.magnitude_kind === 'absolute' ? 'Absolute magnitude' : 'Brightest apparent magnitude', fmtMag(ev.peak_magnitude)],
        ['Duration', fmtNum(ev.duration_s, 1, 's')],
        ['Speed', fmtNum(ev.velocity_kms, 1, 'km/s')],
        ['Height', ev.height_begin_km != null && ev.height_end_km != null
            ? `${Math.round(ev.height_begin_km)} → ${Math.round(ev.height_end_km)} km` : null],
        ['ROVIMEN cameras', `${ev.witnesses.length} at ${ev.station_count} station${ev.station_count === 1 ? '' : 's'}`],
    ];
    if (ev.gmn_cameras && ev.gmn_cameras.length) facts.push(['GMN cameras on the trajectory', String(ev.gmn_cameras.length)]);
    return facts.filter(f => f[1] != null && f[1] !== '—').map(([label, value]) => ({ label, value }));
}

function sheetHtml(kind, item) {
    const isReport = kind === 'report';
    const title    = isReport ? item.title : eventTitle(item);
    const report   = isReport ? { url: item.url, title: item.title, status: item.status } : item.report;
    const facts    = isReport ? item.facts : eventFacts(item);
    const ws       = item.witnesses || [];
    const nClips   = ws.filter(w => w.clip_url).length;
    const nStacks  = ws.filter(w => w.stack_url).length;

    const status = isReport
        ? `<span class="hl-badge ${item.status === 'final' ? 'final' : 'prelim'}">${item.status === 'final' ? 'Final analysis' : 'Preliminary analysis'}</span>`
        : (item.confirmed
            ? '<span class="hl-badge confirmed" title="Solved by the Global Meteor Network">GMN trajectory</span>'
            : '<span class="hl-badge pending" title="Seen by two or more ROVIMEN stations; GMN publishes its trajectory 4-15 days after the night">Awaiting GMN solution</span>');

    const tiles = ws.map((w, i) => `
        <figure class="hl-cam${w.hidden ? ' hd-hidden' : ''}" data-idx="${i}">
          <button type="button" class="hl-cam-img" data-act="zoom" ${w.stack_url ? '' : 'disabled'} title="${w.stack_url ? 'Enlarge the stack' : 'No stack'}" aria-label="${w.stack_url ? 'Enlarge the stack from ' + escHtml(w.camera) : 'No stack from ' + escHtml(w.camera)}">
            ${w.stack_url
                ? `<img src="${escHtml(w.stack_url)}" alt="Stack from ${escHtml(w.camera)}" loading="lazy" onerror="this.parentElement.classList.add('missing');this.remove()">`
                : ''}
            <span class="hl-cam-noimg">No stack</span>
          </button>
          <figcaption>
            ${hiddenNoteHtml(w)}
            <div class="hl-cam-name">
              <span>${escHtml(w.station_label || w.camera)}</span>
              ${w.host_key && state.AUTH_USER
                  ? `<a class="hl-link" href="${escHtml(hlCameraUrl(w))}" title="Open ${escHtml(w.camera)} in the Video DB">${escHtml(w.camera)}</a>`
                  : `<span class="hl-cam-code">${escHtml(w.camera)}</span>`}
            </div>
            <div class="hl-cam-meta">${[
                w.apparent_magnitude != null ? 'mag ' + fmtMag(w.apparent_magnitude) : '',
                w.duration_s != null ? fmtNum(w.duration_s, 1, 's') : '',
            ].filter(Boolean).join(' · ')}</div>
            <div class="hl-cam-actions">
              ${w.clip_url
                  ? '<button type="button" class="hl-btn small primary" data-act="play">Play clip</button>'
                  : '<span class="hl-cam-noclip" title="The clip is not in the archive">No clip</span>'}
              ${w.stack_url ? `<a class="hl-btn small" href="${escHtml(w.stack_url)}" target="_blank" rel="noopener">Open stack</a>` : ''}
              ${!isReport && hideTarget(w) ? hideButtonHtml(w, 'data-act="hide"') : ''}
            </div>
          </figcaption>
        </figure>`).join('');

    return `
      <header class="hl-sheet-head">
        <div class="hl-sheet-kicker">${status}${!isReport && item.fireball ? '<span class="hl-badge fireball">Fireball</span>' : ''}</div>
        <h2 id="hl-sheet-title">${escHtml(title)}</h2>
        <div class="hl-sheet-sub">${escHtml(fmtUtc(item.time_utc))}${item.local_time ? ' · ' + escHtml(item.local_time) : ''}
          · ${escHtml(places(ws).join(', ') || (item.places || []).join(', '))}</div>
        ${isReport && item.summary ? `<p class="hl-sheet-summary">${escHtml(item.summary)}</p>` : ''}
      </header>
      ${facts && facts.length ? `<dl class="hl-facts">${facts.map(f =>
          `<div><dt>${escHtml(f.label)}</dt><dd>${escHtml(f.value)}</dd></div>`).join('')}</dl>` : ''}
      <div class="hl-sheet-actions">
        ${nClips ? `<button type="button" class="hl-btn primary" data-act="play-all">Play all ${nClips} camera${nClips === 1 ? '' : 's'} in sync</button>` : ''}
        ${report ? `<a class="hl-btn research" href="${escHtml(report.url)}">Read the research</a>` : ''}
        ${nStacks > 1 && state.AUTH_USER ? '<button type="button" class="hl-btn" data-act="dl-stacks">Download all stacks</button>' : ''}
      </div>
      <h3 class="hl-sheet-h3">Every camera <span>${ws.length}</span></h3>
      ${ws.length ? `<div class="hl-cam-grid">${tiles}</div>`
                  : '<div class="hl-empty">None of the cameras on this event is visible to you.</div>'}
    `;
}

let _sheetOpener = null;
let _lightboxOpener = null;

function openSheet(kind, item, { push = true } = {}) {
    _sheet = { kind, item };
    const body = el('hl-sheet-body');
    body.innerHTML = sheetHtml(kind, item);
    const ws = item.witnesses || [];
    const title = kind === 'report' ? item.title : eventTitle(item);

    body.querySelector('[data-act="play-all"]')?.addEventListener('click', () => playAll(item, `${title} · ${fmtUtc(item.time_utc)}`));
    body.querySelector('[data-act="dl-stacks"]')?.addEventListener('click', e => downloadStacks(ws, e.currentTarget));
    body.querySelectorAll('.hl-cam').forEach(fig => {
        const idx = parseInt(fig.dataset.idx, 10);
        fig.querySelector('[data-act="play"]')?.addEventListener('click', () => playOne(ws, idx, title));
        fig.querySelector('[data-act="zoom"]')?.addEventListener('click', () => openLightbox(ws[idx]));
        fig.querySelector('[data-act="hide"]')?.addEventListener('click', () => {
            const w = ws[idx];
            toggleHide(hideTarget(w), !w.hidden, resp => hlOnHiddenChange(item, w, resp), { page: 'highlights' });
        });
    });

    const back = el('hl-sheet');
    if (back.hidden) {
        // Remember what opened the sheet so closing returns focus there.
        _sheetOpener = document.activeElement;
        back.hidden = false;
        el('hl-sheet-close').focus();
    }
    document.body.classList.add('hl-noscroll');
    back.querySelector('.hl-sheet').scrollTop = 0;
    if (push) {
        const hash = kind === 'report' ? `#report=${item.event_id}` : `#event=${item.id}`;
        if (location.hash !== hash) history.replaceState(null, '', hash);
    }
    if (kind === 'report') {
        completeReport(item).then(added => {
            if (added && _sheet && _sheet.item === item) openSheet(kind, item, { push: false });
        });
    }
}

function closeSheet() {
    closeLightbox();
    el('hl-sheet').hidden = true;
    document.body.classList.remove('hl-noscroll');
    _sheet = null;
    if (_sheetOpener && _sheetOpener.isConnected) _sheetOpener.focus();
    _sheetOpener = null;
    if (location.hash) history.replaceState(null, '', location.pathname + location.search);
}

function openLightbox(w) {
    if (!w || !w.stack_url) return;
    const lb = el('hl-lightbox');
    lb.querySelector('img').src = w.stack_url;
    lb.querySelector('.hl-lightbox-cap').textContent =
        `${w.station_label || ''} · ${w.camera} · ${fmtUtc(w.time_utc)}`;
    lb.querySelector('img').alt = `Stack from ${w.camera}`;
    _lightboxOpener = document.activeElement;
    lb.hidden = false;
    lb.focus();
}

function closeLightbox() {
    const lb = el('hl-lightbox');
    if (lb.hidden) return false;
    lb.hidden = true;
    lb.querySelector('img').removeAttribute('src');
    if (_lightboxOpener && _lightboxOpener.isConnected) _lightboxOpener.focus();
    _lightboxOpener = null;
    return true;
}

function openFromHash() {
    const m = location.hash.match(/^#(event|report)=([A-Za-z0-9_:.-]+)$/);
    if (!m) return false;
    const item = m[1] === 'report'
        ? _reports.find(r => r.event_id === m[2])
        : _events.find(e => e.id === m[2]);
    if (item) openSheet(m[1], item, { push: false });
    return !!item;
}

// ── Research gallery ──────────────────────────────────────────────────────

// A report card's picture cycles through the report's figures: each holds
// for SLIDE_MS, then fades out to the card's black and the next fades in,
// FADE_MS each way (0.5 s in all). Cards off screen, a hidden tab and
// reduced-motion visitors keep the current picture.
const SLIDE_MS = 3000;
const FADE_MS  = 250;
let _slideTimers = [];
let _slideObserver = null;

export function startSlideshows(grid) {
    _slideTimers.forEach(clearInterval);
    _slideTimers = [];
    _slideObserver?.disconnect();
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const visible = new WeakSet();
    _slideObserver = new IntersectionObserver(entries => entries.forEach(en =>
        en.isIntersecting ? visible.add(en.target) : visible.delete(en.target)));
    grid.querySelectorAll('.hl-rcard-media[data-slides]').forEach(media => {
        const slides = JSON.parse(media.dataset.slides);
        const img = media.querySelector('img');
        if (!img || slides.length < 2) return;
        _slideObserver.observe(media);
        let idx = 0, busy = false;
        _slideTimers.push(setInterval(async () => {
            // Failed figures leave the cycle; with fewer than two left there
            // is nothing to cycle (an empty list asked for slides[NaN]).
            if (slides.length < 2) return;
            if (busy || document.hidden || !visible.has(media)) return;
            busy = true;
            const next = (idx + 1) % slides.length;
            // Decode first, so the fade never lands on a half-loaded picture;
            // a figure that fails to load is dropped from the cycle.
            const pre = new Image();
            pre.src = slides[next];
            try { await pre.decode(); } catch (_) { slides.splice(next, 1); busy = false; return; }
            img.classList.add('is-fading');
            setTimeout(() => {
                img.src = slides[next];
                idx = next;
                img.classList.remove('is-fading');
                busy = false;
            }, FADE_MS);
        }, SLIDE_MS));
    });
}

function renderReports() {
    const wrap = el('hl-research');
    const grid = el('hl-research-grid');
    if (!_reports.length) { wrap.hidden = true; return; }
    wrap.hidden = false;
    grid.innerHTML = _reports.map((r, i) => {
        const best = hlBestStack(r.witnesses);
        const cover = r.cover_url || best?.stack_url;
        const slides = (r.slides || []).length > 1 ? r.slides : null;
        const nClips = r.witnesses.filter(w => w.clip_url).length;
        return `
        <article class="hl-rcard" data-idx="${i}">
          <button type="button" class="hl-rcard-media${slides ? ' is-slideshow' : ''}" data-act="sheet" aria-label="Show every camera"
                  ${slides ? `data-slides="${escHtml(JSON.stringify(slides))}"` : ''}>
            ${cover ? `<img src="${escHtml(cover)}" alt="" loading="lazy" onerror="this.remove()">` : '<div class="hl-mosaic-empty"></div>'}
            <span class="hl-media-count">${r.witnesses.length} camera${r.witnesses.length === 1 ? '' : 's'}</span>
          </button>
          <div class="hl-rcard-body">
            <div class="hl-rcard-kicker">
              <span class="hl-chip-status ${r.status === 'final' ? 'final' : ''}">${r.status === 'final' ? 'Final' : 'Preliminary'}</span>
              <span>${escHtml(fmtWhen(r.time_utc))}</span>
            </div>
            <h3><a href="${escHtml(r.url)}">${escHtml(r.title)}</a></h3>
            <p class="hl-rcard-summary">${escHtml(r.summary)}</p>
            <div class="hl-rcard-places">${escHtml((r.places || []).join(' · '))}</div>
            <div class="hl-rcard-actions">
              <a class="hl-btn primary" href="${escHtml(r.url)}">Read the research</a>
              ${nClips ? `<button type="button" class="hl-btn" data-act="play-all">Play ${nClips} camera${nClips === 1 ? '' : 's'}</button>` : ''}
              <button type="button" class="hl-btn quiet" data-act="sheet">Stacks and clips</button>
            </div>
          </div>
        </article>`;
    }).join('');

    grid.querySelectorAll('.hl-rcard').forEach(card => {
        const r = _reports[parseInt(card.dataset.idx, 10)];
        card.querySelectorAll('[data-act="sheet"]').forEach(b => b.addEventListener('click', () => openSheet('report', r)));
        card.querySelector('[data-act="play-all"]')?.addEventListener('click', async e => {
            const btn = e.currentTarget;
            btn.disabled = true;
            await completeReport(r);
            btn.disabled = false;
            playAll(r, `${r.title} · ${fmtUtc(r.time_utc)}`);
        });
    });
    startSlideshows(grid);
}

async function loadReports() {
    try {
        const resp = await fetch('/api/highlights/reports');
        if (!resp.ok) return;
        _reports = (await resp.json()).reports || [];
        renderReports();
    } catch (_) { /* the gallery stays hidden */ }
}

// ── Events grid ───────────────────────────────────────────────────────────

const _FILTERS = {
    all:       () => true,
    fireball:  ev => ev.fireball,
    report:    ev => !!ev.report,
    confirmed: ev => ev.confirmed,
};

/** One event card. The image is the brightest camera's stack; the
 *  magnitude is the headline, coloured by how bright it was. */
function eventCardHtml(ev, rank, lead) {
    const best = hlBestStack(ev.witnesses);
    const pl = places(ev.witnesses);
    const shower = showerFullName(ev.shower || 'SPO');
    const kind = ev.fireball ? `Fireball · ${shower}` : shower;
    const status = ev.report ? '<span class="hl-chip-status research">Research</span>'
        : ev.confirmed ? '' : '<span class="hl-chip-status" title="GMN publishes its trajectory 4-15 days after the night">Awaiting GMN</span>';
    return `
    <article class="hl-ev${lead ? ' is-lead' : ''}${ev.hidden ? ' hd-hidden' : ''}" data-id="${escHtml(ev.id)}" tabindex="0" role="button"
             aria-label="${escHtml(eventTitle(ev))}, ${escHtml(fmtUtc(ev.time_utc))}: show every camera">
      <div class="hl-mosaic n1">
        ${best ? `<img src="${escHtml(best.stack_url)}" alt="" loading="${lead ? 'eager' : 'lazy'}" onerror="this.style.visibility='hidden'">`
               : '<div class="hl-mosaic-empty"></div>'}
        <span class="hl-rank">#${rank}</span>
        <span class="hl-media-count">${ev.witnesses.length} camera${ev.witnesses.length === 1 ? '' : 's'}</span>
      </div>
      <div class="hl-ev-body">
        <div class="hl-ev-top">
          <span class="hl-ev-mag ${hlMagTone(ev.peak_magnitude)}" title="${ev.magnitude_kind === 'absolute' ? 'Absolute magnitude (GMN)' : 'Brightest apparent magnitude'}">${fmtMag(ev.peak_magnitude)}</span>
          ${status}
        </div>
        <div class="hl-ev-title">${escHtml(kind)}${ev.duration_s != null ? ' · ' + fmtNum(ev.duration_s, 1, 's') : ''}</div>
        <div class="hl-ev-meta" title="${escHtml(pl.join(', '))}">${escHtml(fmtWhen(ev.time_utc))} · ${escHtml(placesShort(pl))}</div>
      </div>
    </article>`;
}

function renderEvents() {
    const grid = el('hl-grid');
    const shown = _events.filter(ev => ev.witnesses.length > 0)
        .filter(_FILTERS[_filter] || _FILTERS.all);
    const note = el('hl-filter-note');
    note.textContent = _events.length ? `${shown.length} of ${_events.length} events` : '';

    if (!shown.length) {
        grid.innerHTML = `<div class="hl-empty">${_events.length
            ? 'No event in this range matches the filter.'
            : 'No multi-station event with a clip in this range.'}</div>`;
        return;
    }
    // The brightest event leads the grid as a large card.
    grid.innerHTML = shown.map((ev, i) => eventCardHtml(ev, _events.indexOf(ev) + 1, i === 0)).join('');
}

/** One click / key handler for every event card (the grid re-renders often). */
function wireEventGrid() {
    const grid = el('hl-grid');
    const open = (card) => {
        const ev = card && _events.find(e => e.id === card.dataset.id);
        if (ev) openSheet('event', ev);
    };
    grid.addEventListener('click', e => open(e.target.closest('.hl-ev:not(.skeleton)')));
    grid.addEventListener('keydown', e => {
        if ((e.key !== 'Enter' && e.key !== ' ') || !e.target.matches?.('.hl-ev')) return;
        e.preventDefault();
        open(e.target);
    });
}

// ── Stats ─────────────────────────────────────────────────────────────────

function renderStats(data) {
    const map = {
        'hl-val-detections':   data.total_detections?.toLocaleString('en-US') ?? '—',
        'hl-val-events':       data.dual_station_events?.toLocaleString('en-US') ?? '—',
        'hl-val-orbits':       data.new_orbits?.toLocaleString('en-US') ?? '—',
        'hl-val-orbits-month': data.orbits_month_to_date?.toLocaleString('en-US') ?? '—',
        'hl-val-shower':       showerFullName(data.main_shower),
        'hl-val-cameras':      data.active_cameras ?? '—',
        'hl-val-locations':    data.active_locations ?? '—',
        'hl-val-coverage':     data.coverage_90km != null ? data.coverage_90km.toFixed(1) + '%' : '—',
    };
    for (const [id, val] of Object.entries(map)) {
        const node = el(id);
        if (node) { node.textContent = val; node.classList.remove('loading'); }
    }
}

function renderMonthly(data) {
    const wrap = el('hl-months-wrap');
    const totals = data.monthly_totals || [];
    if (totals.length <= 1) { wrap.hidden = true; return; }
    el('hl-months-tbody').innerHTML = totals.map(row => `
        <tr><th scope="row">${escHtml(row.month)}</th>
            <td>${(row.detections || 0).toLocaleString('en-US')}</td>
            <td>${(row.events || 0).toLocaleString('en-US')}</td></tr>`).join('');
    wrap.hidden = false;
}

// ── Fetch ─────────────────────────────────────────────────────────────────

function isoDay(d) { return d.toISOString().slice(0, 10); }

function setLoading(on) {
    const btn = el('hl-submit');
    btn.disabled = on;
    btn.textContent = on ? 'Loading…' : 'Load';
    el('hl-stats').setAttribute('aria-busy', on ? 'true' : 'false');
    document.querySelectorAll('.hl-stat-value').forEach(n => n.classList.toggle('loading', on));
    if (on) {
        el('hl-grid').innerHTML = Array.from({ length: 8 },
            () => '<div class="hl-ev skeleton"><div class="hl-mosaic"></div><div class="hl-ev-body"></div></div>').join('');
    }
}

let _queryToken = 0;

// A cold night costs the server a few seconds (its detections payload), so a
// whole range in one request ran past the proxy's 30 s timeout. The page asks
// for CHUNK_NIGHTS nights at a time, newest first, and merges as they arrive.
const CHUNK_NIGHTS = 4;
const TOP_EVENTS   = 24;

export function hlChunks(start, end, size = CHUNK_NIGHTS) {
    const out = [];
    let hi = new Date(end + 'T00:00:00Z');
    const lo = new Date(start + 'T00:00:00Z');
    while (hi >= lo) {
        const from = new Date(hi);
        from.setUTCDate(hi.getUTCDate() - (size - 1));
        out.push([isoDay(from < lo ? lo : from), isoDay(hi)]);
        hi = new Date(from);
        hi.setUTCDate(from.getUTCDate() - 1);
    }
    return out;
}

/** Same order as highlight_events.rank_key on the server. */
export function hlRank(a, b) {
    if (!!a.confirmed !== !!b.confirmed) return a.confirmed ? -1 : 1;
    const ma = a.peak_magnitude ?? Infinity, mb = b.peak_magnitude ?? Infinity;
    if (ma !== mb) return ma - mb;
    const da = a.duration_s || 0, db = b.duration_s || 0;
    if (da !== db) return db - da;
    return (b.witnesses || []).length - (a.witnesses || []).length;
}

/** Merge per-chunk payloads into one range payload. */
export function hlMerge(parts) {
    const sum = k => parts.reduce((n, p) => n + (p[k] || 0), 0);
    const showers = {};
    const months  = {};
    for (const p of parts) {
        for (const [k, v] of Object.entries(p.shower_counts || {})) showers[k] = (showers[k] || 0) + v;
        for (const m of p.monthly_totals || []) {
            const t = months[m.month] || (months[m.month] = { month: m.month, detections: 0, events: 0 });
            t.detections += m.detections || 0;
            t.events     += m.events || 0;
        }
    }
    const pick = obj => Object.keys(obj).reduce((best, k) => (best == null || obj[k] > obj[best] ? k : best), null);
    const nonSpo = Object.fromEntries(Object.entries(showers).filter(([k]) => k && k !== 'SPO'));
    const last = parts[0] || {};
    return {
        total_detections:     sum('total_detections'),
        dual_station_events:  sum('dual_station_events'),
        new_orbits:           sum('new_orbits'),
        orbits_month_to_date: last.orbits_month_to_date,
        main_shower:          pick(nonSpo) || pick(showers) || 'SPO',
        active_cameras:       last.active_cameras,
        active_locations:     last.active_locations,
        coverage_90km:        last.coverage_90km,
        monthly_totals:       Object.values(months).sort((x, y) => x.month.localeCompare(y.month)),
        events:               parts.flatMap(p => p.events || []).sort(hlRank).slice(0, TOP_EVENTS),
    };
}

async function fetchChunk(from, to) {
    for (let attempt = 0; ; attempt++) {
        const resp = await fetch(withHidden(`/api/highlights/data?start=${from}&end=${to}`));
        if (resp.ok) return resp.json();
        // A 502/504 still finishes on the server and fills its cache.
        if (attempt < 2 && resp.status >= 500) { await new Promise(r => setTimeout(r, 2000)); continue; }
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.error || `HTTP ${resp.status}`);
    }
}

async function runQuery() {
    const start = el('hl-start').value;
    const end   = el('hl-end').value;
    if (!start || !end) { _toast('Pick a start and end date', 'error'); return; }
    if (start > end)    { _toast('The start date must be before the end date', 'error'); return; }

    const days = Math.round((new Date(end) - new Date(start)) / 86400000) + 1;
    if (days > 366) { _toast('Pick at most one year', 'error'); return; }
    document.querySelectorAll('.hl-presets .hl-chip').forEach(c => {
        c.classList.toggle('active', +c.dataset.days === days && end === isoDay(new Date()));
    });
    const label = `${start} to ${end} (${days} night${days === 1 ? '' : 's'})`;
    el('hl-range-label').textContent = label;
    const qs = new URLSearchParams(location.search);
    qs.set('start', start); qs.set('end', end);
    history.replaceState(null, '', `${location.pathname}?${qs}${location.hash}`);

    const token = ++_queryToken;
    const chunks = hlChunks(start, end);
    const parts = [];
    let failed = 0;
    setLoading(true);
    try {
        for (let i = 0; i < chunks.length; i++) {
            if (chunks.length > 1) el('hl-range-label').textContent = `${label} · loading ${i + 1}/${chunks.length}`;
            try {
                parts.push(await fetchChunk(...chunks[i]));
            } catch (e) {
                failed++;
                if (token !== _queryToken) return;
                if (failed === chunks.length) throw e;
                continue;
            }
            if (token !== _queryToken) return;
            const data = hlMerge(parts);
            _events = data.events;
            renderStats(data);
            renderMonthly(data);
            renderEvents();
            if (!_sheet) openFromHash();
        }
        el('hl-range-label').textContent = failed
            ? `${label} · ${failed} of ${chunks.length} parts failed to load`
            : label;
    } catch (e) {
        if (token !== _queryToken) return;
        el('hl-grid').innerHTML = `<div class="hl-error">Could not load highlights: ${escHtml(String(e.message))}</div>`;
    } finally {
        if (token === _queryToken) setLoading(false);
    }
}

function setRange(days) {
    const end = new Date();
    const start = new Date(end);
    start.setUTCDate(end.getUTCDate() - (days - 1));
    el('hl-start').value = isoDay(start);
    el('hl-end').value   = isoDay(end);
    _markPreset(days);
    runQuery();
}

// Highlight the preset chip that matches the range (none for a custom one).
function _markPreset(days) {
    document.querySelectorAll('.hl-presets .hl-chip').forEach(c => {
        const on = +c.dataset.days === days;
        c.classList.toggle('active', on);
        c.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
}

/** "Show hidden" chip in the event filters' toolbar (sessions that may hide,
 *  dashboard-hidden.js toggleHtml): the events come from the server with or
 *  without hidden detections, so a flip reloads the range. */
function hlInitHiddenToggle() {
    const bar = document.querySelector('.hl-filters');
    if (!bar || el('hl-hidden-toggle')) return;
    const tpl = document.createElement('template');
    tpl.innerHTML = toggleHtml('hl-hidden-toggle', 0, '', 'hl-chip hd-chip');
    const b = tpl.content.firstElementChild;
    if (!b) return;
    b.classList.toggle('active', getShowHidden());
    bar.insertBefore(b, el('hl-filter-note'));
    wireToggle(b, on => { b.classList.toggle('active', on); runQuery(); });
}

// ── Init ──────────────────────────────────────────────────────────────────

function init() {
    wireEventGrid();
    document.querySelectorAll('.hl-presets .hl-chip').forEach(c =>
        c.addEventListener('click', () => setRange(+c.dataset.days)));
    document.querySelectorAll('.hl-filters .hl-chip[data-filter]').forEach(c =>
        c.addEventListener('click', () => {
            _filter = c.dataset.filter;
            document.querySelectorAll('.hl-filters .hl-chip[data-filter]').forEach(x => {
                x.classList.toggle('active', x === c);
                x.setAttribute('aria-pressed', x === c ? 'true' : 'false');
            });
            renderEvents();
        }));
    el('hl-submit').addEventListener('click', () => { _markPreset(null); runQuery(); });
    ['hl-start', 'hl-end'].forEach(id =>
        el(id).addEventListener('keydown', e => { if (e.key === 'Enter') runQuery(); }));

    const back = el('hl-sheet');
    el('hl-sheet-close').addEventListener('click', closeSheet);
    back.addEventListener('click', e => { if (e.target === back) closeSheet(); });
    el('hl-lightbox').addEventListener('click', closeLightbox);
    // Keep Tab inside the open sheet (it is aria-modal).
    back.addEventListener('keydown', e => {
        if (e.key !== 'Tab' || back.hidden) return;
        if (!el('hl-lightbox').hidden) { e.preventDefault(); return; }
        const f = [...back.querySelectorAll('.hl-sheet button:not([disabled]), .hl-sheet a[href], .hl-sheet [tabindex="0"]')]
            .filter(n => n.offsetParent !== null);
        if (!f.length) return;
        const first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    });
    document.addEventListener('keydown', e => {
        if (e.key !== 'Escape' || back.hidden || e.defaultPrevented) return;
        // A player opened from the sheet closes first (it handles its own Esc).
        if (document.querySelector('.vm-backdrop.open, .mdm-backdrop.open')) return;
        if (!closeLightbox()) closeSheet();
    });
    window.addEventListener('hashchange', () => { if (!openFromHash() && _sheet) closeSheet(); });

    loadReports().then(() => { if (!_sheet) openFromHash(); });

    const qs = new URLSearchParams(location.search);
    const re = /^\d{4}-\d{2}-\d{2}$/;
    if (re.test(qs.get('start') || '') && re.test(qs.get('end') || '')) {
        el('hl-start').value = qs.get('start');
        el('hl-end').value   = qs.get('end');
        runQuery();
    } else {
        setRange(30);
    }
}

(async function initAuth() {
    try {
        // The shared players hide downloads for anonymous visitors off
        // state.AUTH_USER, which the session request writes before it resolves.
        const auth = await fetchAuthStatus();
        if (auth && auth.admin) _isAdmin = true;
        hlInitHiddenToggle();
        if (_events.length) renderEvents();
        if (_sheet) openSheet(_sheet.kind, _sheet.item, { push: false });
    } catch (e) {}
    // Public gallery; admins get caption / period / remove on each card.
    mountHofGallery({ admin: _isAdmin });
})();


init();
