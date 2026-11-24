import { escHtml, fetchAuthStatus } from './dashboard-common.js';
import { openMaskEditor } from './dashboard-mask-editor.js';

/* The per-camera plots, in display order. Each resolves through
   /api/calibration/plot/<kind>/<camera>, which reads the dashboard's local
   archive index only -- no station round-trip, no fan-out that scales with
   camera count (PR #878 review). Adding a plot here and to
   CALIBRATION_PLOT_SUFFIXES in routes/archive.py is the whole change. */
const PLOTS = [
  { kind: 'ff_intervals',        cls: 'ff',     label: 'FF intervals',           empty: 'No FF intervals plot' },
  { kind: 'astrometry_residual', cls: 'resid',  label: 'Astrometry residual',    empty: 'No residual plot' },
  { kind: 'astrometry_report',   cls: 'report', label: 'Astrometry calibration', empty: 'No astrometry report' },
];

export function parseNightDate(s) {
  if (!/^\d{8}$/.test(s || '')) return null;
  return Date.UTC(+s.slice(0, 4), +s.slice(4, 6) - 1, +s.slice(6, 8));
}

export function nightsBehind(date, expectedDate) {
  const a = parseNightDate(date);
  const b = parseNightDate(expectedDate);
  if (a === null || b === null) return null;
  return Math.round((b - a) / 86400000);
}

export function formatNightDate(s) {
  return /^\d{8}$/.test(s || '') ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}` : s;
}

/* The card's freshness line. This is the page's whole answer to "am I
   looking at last night or at last month": the plots themselves carry no
   visible date, so without it a camera that stopped uploading four weeks ago
   renders identically to one that processed this morning. Deliberately
   replaces the platepar-age line that used to sit here, which measured the
   epoch of the last manual SkyFit solve -- a number that runs to hundreds of
   days on healthy cameras, because RMS's ApplyRecalibrate refits nightly and
   never touches it. That result is already on the card, as the drift line. */
export function nightStatus(results) {
  const ok = results.filter(r => r && r.status === 'ok' && r.date);
  const expected = results.find(r => r && r.expected_date)?.expected_date || null;
  if (!ok.length) {
    return { text: 'Night: no plots archived', cls: 'cal-stale-bad' };
  }
  const newest = ok.map(r => r.date).sort().pop();
  const behind = nightsBehind(newest, expected);
  if (behind === null || behind <= 0) {
    return { text: `Night ${formatNightDate(newest)}`, cls: '' };
  }
  const plural = behind === 1 ? 'night' : 'nights';
  return {
    text: `Night ${formatNightDate(newest)} — ${behind} ${plural} behind`,
    cls: behind >= 3 ? 'cal-stale-bad' : 'cal-stale-warn',
  };
}

function plotsRowHtml() {
  return PLOTS.map(p => `
        <div class="cal-plot-wrap cal-plot-${p.cls}" data-empty-label="${escHtml(p.empty)}">
          <img class="cal-plot-img" data-kind="${p.kind}" loading="lazy" alt="${escHtml(p.label)}">
        </div>`).join('');
}

/* A spectroscopy camera has no RMS pipeline: no platepar, no config
   directory, no nightly recalibration, no mask. Every RMS-shaped thing on
   this page is inapplicable to it, so the card says so once instead of
   rendering three empty plot tiles, a red "no plots archived", and three
   buttons that can only ever answer "could not resolve this camera's RMS
   config directory". */
function cardHtml(host, cam, spectro = false) {
  if (spectro) {
    return `
    <div class="cal-card cal-card-spectro" data-host="${escHtml(host)}" data-cam="${escHtml(cam)}">
      <div class="cal-card-title"><span class="cal-card-cam" translate="no">${escHtml(cam)}</span> <span class="cal-card-host" translate="no">${escHtml(host)}</span></div>
      <div class="cal-stack-wrap cal-zoomable" role="button" tabindex="0" aria-label="Open ${escHtml(cam)} night stack">
        <img class="cal-stack-img" src="/api/latest_stack/${host}/${cam}" loading="lazy" alt=""
             onerror="this.style.display='none'">
        <div class="cal-mask-img cal-mask-tint" style="display:none"></div>
      </div>
      <div class="cal-facts">
        <div class="cal-fact" data-field="night">Spectroscopy camera — no RMS calibration</div>
      </div>
    </div>`;
  }
  return `
    <div class="cal-card" data-host="${escHtml(host)}" data-cam="${escHtml(cam)}">
      <div class="cal-card-title"><span class="cal-card-cam" translate="no">${escHtml(cam)}</span> <span class="cal-card-host" translate="no">${escHtml(host)}</span></div>
      <div class="cal-stack-wrap cal-zoomable" role="button" tabindex="0" aria-label="Open ${escHtml(cam)} night stack">
        <img class="cal-stack-img" src="/api/latest_stack/${host}/${cam}" loading="lazy" alt=""
             onerror="this.style.display='none'">
        <div class="cal-mask-img cal-mask-tint" style="display:none"></div>
      </div>
      <div class="cal-plots-row">${plotsRowHtml()}</div>
      <div class="cal-facts">
        <div class="cal-fact" data-field="night">Night: …</div>
        <div class="cal-fact" data-field="residual">Calibration: …</div>
        <div class="cal-fact" data-field="masked">Mask: …</div>
      </div>
      <div class="cal-ops" data-field="ops" style="display:none">
        <div class="cal-ops-row">
          <button type="button" class="cal-rebase-btn">Re-base pointing</button>
          <button type="button" class="cal-autofit-btn">Re-solve from scratch</button>
          <button type="button" class="cal-mask-btn">Edit mask</button>
        </div>
        <div class="cal-autofit-result" style="display:none"></div>
      </div>
    </div>`;
}

async function fillPlots(card, cam) {
  const results = await Promise.all(PLOTS.map(async p => {
    const wrap = card.querySelector(`.cal-plot-${p.cls}`);
    const img = wrap?.querySelector('.cal-plot-img');
    let r = null;
    try {
      r = await (await fetch(`/api/calibration/plot/${p.kind}/${cam}`)).json();
    } catch (e) {
      r = null;
    }
    if (r && r.status === 'ok' && r.filename && img) {
      const url = `/api/archive/file/${cam}/${r.date}/rms/${encodeURIComponent(r.filename)}`;
      // The index says the plot is archived, which is not the same as the
      // bytes arriving: a stalled storagebox mount answers 503. Fall back to
      // the empty state rather than leaving a broken image showing its alt
      // text, which reads as a rendering bug rather than as missing data.
      img.addEventListener('error', () => wrap?.classList.add('cal-empty'));
      img.src = url;
      img.classList.add('cal-zoomable');
      img.tabIndex = 0;
      img.setAttribute('role', 'button');
      img.addEventListener('click', () => openZoom(url, `${cam} — ${p.label} (${formatNightDate(r.date)})`));
    } else {
      wrap?.classList.add('cal-empty');
    }
    return r;
  }));

  const el = card.querySelector('[data-field="night"]');
  if (el) {
    const status = nightStatus(results);
    el.textContent = status.text;
    if (status.cls) el.classList.add(status.cls);
  }
  return results;
}

async function fillResidual(card, host, cam) {
  const el = card.querySelector('[data-field="residual"]');
  if (!el) return;
  try {
    const r = await (await fetch(`/api/calibration_summary/${host}/${cam}`)).json();
    if (r.status === 'ok') {
      // recalib_ok_pct first: it is the health signal. A camera whose frames
      // mostly fail to recalibrate needs attention regardless of how small
      // the drift of the ones that succeeded looks, and RO001M (17% ok, 0.16
      // arcmin/h) is exactly that camera.
      const parts = [`${r.recalib_ok_pct}% recalib ok`];
      if (r.pointing_arcmin_per_h != null) {
        parts.push(`${r.pointing_arcmin_per_h}'/h pointing`);
      }
      if (r.rotation_arcmin_per_h != null) {
        parts.push(`${r.rotation_arcmin_per_h}'/h rotation`);
      }
      el.textContent = parts.join(', ');
      const flagged = r.recalib_ok_pct < 80
        || r.pointing_arcmin_per_h > 6 || r.rotation_arcmin_per_h > 3;
      if (flagged) el.classList.add('cal-fact-bad');
    } else if (r.status === 'no_data') {
      el.textContent = 'Calibration: no data (cloudy / offline?)';
    } else {
      el.textContent = 'Calibration: unavailable';
    }
  } catch (e) {
    el.textContent = 'Drift: unavailable';
  }
}

/* mask.bmp is 0=masked-out, >0=active sky -- and on a typical real camera
   the ACTIVE region is the vast majority of the frame (only a thin strip at
   the bottom is usually excluded). CSS mask-image's default luminance
   reading shows the tint wherever the source is BRIGHT, i.e. over the
   active region -- which on a real mask covers nearly the whole picture,
   not a small highlighted patch (this is exactly the "why is the whole
   image magenta" bug: obvious once seen on a real mask, easy to miss on a
   synthetic 50/50 test one). What we actually want is the opposite: tint
   only the small EXCLUDED area, leaving the active sky clear.
   Invert the pixel values via <canvas>'s 2D filter (a plain per-channel
   255-v negation, not a hue operation -- unlike sepia/hue-rotate it works
   correctly on pure black/white, confirmed while debugging an earlier
   attempt) so bright/dark swap before the mask is ever applied. Returns a
   data: URL so both the card and the zoom modal can reuse the exact same
   inverted image without inverting twice. */
function loadMaskImage(url) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error('mask image failed to load'));
    img.src = url;
  });
}

/* Same threshold the station applies when it binarises an uploaded mask on
   save (station_api.py api_mask_write: `0 if v < 128 else 255`), so a
   camera whose mask was drawn with an antialiased brush edge is judged
   "masked" here exactly where RMS itself would treat it as excluded sky.
   Reads the red channel only -- the BMP RMS writes is greyscale (R=G=B), so
   there's no luminance math to do, just one pass over the pixel buffer. */
export function maskedPercentFromCanvas(ctx, w, h) {
  if (!w || !h) return null;
  let data;
  try {
    data = ctx.getImageData(0, 0, w, h).data;
  } catch (e) {
    return null;   // canvas tainted -- shouldn't happen same-origin, but don't crash the card over it
  }
  let maskedPx = 0;
  for (let i = 0; i < data.length; i += 4) {
    if (data[i] < 128) maskedPx++;
  }
  return 100 * maskedPx / (w * h);
}

async function setupMask(card, host, cam) {
  const maskDiv = card.querySelector('.cal-mask-img');
  const factEl = card.querySelector('[data-field="masked"]');
  if (!maskDiv) return;
  try {
    // Cache-bust so a mask just saved from the editor is the one shown.
    const img = await loadMaskImage(`/api/mask_image/${host}/${cam}?t=${Date.now()}`);
    const canvas = document.createElement('canvas');
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('no 2d context');

    // Drawn plain first so the percentage reads the mask's real pixel
    // values, before the invert filter below turns it into the overlay's
    // tint source (see the comment above this function's old body for why
    // the overlay needs inverting -- unrelated to the percentage itself).
    ctx.drawImage(img, 0, 0);
    if (factEl) {
      const pct = maskedPercentFromCanvas(ctx, canvas.width, canvas.height);
      factEl.textContent = pct == null ? 'Mask: unreadable' : `Mask: ${pct.toFixed(1)}% masked`;
    }

    ctx.filter = 'invert(1)';
    ctx.drawImage(img, 0, 0);
    const dataUrl = canvas.toDataURL();
    maskDiv.style.maskImage = `url("${dataUrl}")`;
    maskDiv.style.webkitMaskImage = `url("${dataUrl}")`;
    maskDiv.dataset.maskUrl = dataUrl;
    maskDiv.style.display = '';
  } catch (e) {
    maskDiv.style.display = 'none';   // no mask for this camera
    if (factEl) factEl.textContent = 'Mask: no mask found';
  }
}

/* ── Zoom modal ───────────────────────────────────────────────────────────
   Scroll-zoom + drag-pan + download, matching the overview page's plot
   modal, so a plot read at thumbnail size on the card can actually be read.
   The one thing it adds is the mask overlay, which has to survive zooming
   with the stack image underneath it. */
let _pz = { scale: 1, tx: 0, ty: 0 };

function applyTransform() {
  const wrap = document.getElementById('cal-modal-imgwrap');
  if (!wrap) return;
  wrap.style.transform = `translate(${_pz.tx}px,${_pz.ty}px) scale(${_pz.scale})`;
  const img = document.getElementById('cal-modal-img');
  if (img) img.style.cursor = _pz.scale > 1 ? 'grab' : 'zoom-out';
}

export function openZoom(src, label, maskSrc = null) {
  const modal = document.getElementById('cal-modal');
  const img = document.getElementById('cal-modal-img');
  const mask = document.getElementById('cal-modal-mask');
  const title = document.getElementById('cal-modal-title');
  const dl = document.getElementById('cal-modal-dl');
  if (!modal || !img || !src) return;
  img.src = src;
  if (title) title.textContent = label || '';
  if (dl) dl.href = src;
  _pz = { scale: 1, tx: 0, ty: 0 };
  applyTransform();
  if (mask) {
    if (maskSrc) {
      mask.style.maskImage = `url('${maskSrc}')`;
      mask.style.webkitMaskImage = `url('${maskSrc}')`;
      mask.style.display = '';
    } else {
      mask.style.display = 'none';
      mask.style.maskImage = '';
      mask.style.webkitMaskImage = '';
    }
  }
  if (img) img.alt = label || '';
  _zoomReturnFocus = document.activeElement;
  modal.classList.add('open');
  document.getElementById('cal-modal-close')?.focus();
}

// The thumbnail that opened the zoom gets focus back when it closes.
let _zoomReturnFocus = null;

function closeZoom() {
  const modal = document.getElementById('cal-modal');
  if (!modal?.classList.contains('open')) return;
  modal.classList.remove('open');
  _zoomReturnFocus?.focus?.();
  _zoomReturnFocus = null;
}

function wireZoomModal() {
  const modal = document.getElementById('cal-modal');
  const img = document.getElementById('cal-modal-img');
  document.getElementById('cal-modal-close')?.addEventListener('click', closeZoom);
  modal?.addEventListener('click', (e) => { if (e.target === modal) closeZoom(); });
  // Click closes only at 1:1 -- once zoomed in, a click is the start of a pan.
  img?.addEventListener('click', () => { if (_pz.scale <= 1) closeZoom(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && modal?.classList.contains('open')) closeZoom();
    // Enter / Space open a focused thumbnail (role="button") like a click.
    if ((e.key === 'Enter' || e.key === ' ') && e.target?.classList?.contains('cal-zoomable')
        && e.target.getAttribute('role') === 'button') {
      e.preventDefault();
      e.target.click();
    }
  });

  img?.addEventListener('wheel', (e) => {
    e.preventDefault();
    _pz.scale = Math.max(1, Math.min(10, _pz.scale * (e.deltaY < 0 ? 1.2 : 0.83)));
    if (_pz.scale <= 1.05) _pz = { scale: 1, tx: 0, ty: 0 };
    applyTransform();
  }, { passive: false });

  let dragging = false, sx = 0, sy = 0;
  img?.addEventListener('mousedown', (e) => {
    if (_pz.scale <= 1) return;
    e.preventDefault();
    dragging = true; sx = e.clientX - _pz.tx; sy = e.clientY - _pz.ty;
  });
  document.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    _pz.tx = e.clientX - sx; _pz.ty = e.clientY - sy;
    applyTransform();
  });
  document.addEventListener('mouseup', () => { dragging = false; });
}

function wireStackZoom(card, host, cam) {
  const wrap = card.querySelector('.cal-stack-wrap');
  const stackImg = card.querySelector('.cal-stack-img');
  const maskDiv = card.querySelector('.cal-mask-img');
  wrap?.addEventListener('click', () => {
    if (!stackImg || stackImg.style.display === 'none') return;
    const maskSrc = (maskDiv && maskDiv.style.display !== 'none') ? maskDiv.dataset.maskUrl : null;
    openZoom(stackImg.src, `${cam} — night stack`, maskSrc);
  });
}

function wireMaskButton(card, host, cam) {
  card.querySelector('.cal-mask-btn')?.addEventListener('click', () => {
    openMaskEditor({
      host, cam,
      stackUrl: `/api/latest_stack/${host}/${cam}`,
      // Cache-busted: reopening the editor after a save must start from the
      // mask that is now on the camera, not the browser's copy of the one it
      // replaced.
      maskUrl: `/api/mask_image/${host}/${cam}?t=${Date.now()}`,
      // Refresh the card's overlay from the station so what the card shows
      // is the mask that actually landed, not the one we just drew.
      onSaved: () => setupMask(card, host, cam),
    });
  });
}

/* ── Platepar auto-fit ────────────────────────────────────────────────────
   Operator-initiated live calls to the station -- sanctioned under issue
   #880's push-first doctrine (routine page rendering must stay archive-
   only; this is the opposite of routine, it's a deliberate button press).
   The trigger only ever produces a STAGED candidate; nothing touches the
   live platepar until Accept is clicked. */

async function triggerAutofit(host, cam) {
  const resp = await fetch(`/api/calibration/autofit/${host}/${cam}`, { method: 'POST' });
  return resp.json();
}

async function triggerRebase(host, cam) {
  const resp = await fetch(`/api/calibration/rebase_pointing/${host}/${cam}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
  });
  return resp.json();
}

async function fetchAutofitStatus(host, cam) {
  const resp = await fetch(`/api/calibration/autofit_status/${host}/${cam}`);
  return resp.json();
}

async function acceptAutofit(host, cam, candidateId, acceptToken) {
  const resp = await fetch(`/api/calibration/autofit_accept/${host}/${cam}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ candidate_id: candidateId, accept_token: acceptToken }),
  });
  return resp.json();
}

function renderAutofitResult(container, host, cam, result) {
  container.style.display = '';

  if (result.status === 'running') {
    container.innerHTML = '<span class="cal-autofit-status">Running…</span>';
    return;
  }
  if (result.status === 'no_run') {
    container.style.display = 'none';
    container.innerHTML = '';
    return;
  }
  if (result.status !== 'done') {
    const detail = escHtml(result.detail || result.error || result.status || 'unknown error');
    container.innerHTML = `<span class="cal-autofit-status cal-autofit-error">${escHtml(result.status || 'error')}: ${detail}</span>`;
    return;
  }

  // A re-base reports how far the platepar moves, not a residual: it runs no
  // fit, so there is no before/after goodness to compare. What matters is the
  // size of the correction and that the distortion model came through
  // untouched.
  if (result.mode === 'rebase') {
    const rot = result.rotation_shift_arcmin != null
      ? `, rotation ${result.rotation_shift_arcmin}'` : '';
    const scale = result.scale_change_pct != null
      ? `, scale ${result.scale_change_pct > 0 ? '+' : ''}${result.scale_change_pct}%` : '';
    container.innerHTML = `
      <div class="cal-autofit-status">Pointing moves ${escHtml(String(result.pointing_shift_arcmin))}'`
        + `${escHtml(rot)}${escHtml(scale)} — from ${escHtml(String(result.source_night))}, `
        + `${result.n_frames_recalibrated}/${result.n_frames_total} frames, distortion unchanged</div>
      <button type="button" class="cal-autofit-accept">Accept</button>
      <button type="button" class="cal-autofit-discard">Discard</button>`;
    wireAcceptDiscard(container, host, cam, result);
    return;
  }

  // old/new_n_matched surfaced alongside the residual (PR #884 review): a
  // fit biased toward the field center matches fewer edge stars and
  // reports a lower residual purely from sample shrinkage, which looks
  // like an improvement without being one -- showing star counts lets an
  // operator catch that before clicking Accept.
  const oldR = result.old_residual_arcmin != null
    ? `${result.old_residual_arcmin}' / ${result.old_n_matched} stars` : '?';
  const newR = result.new_residual_arcmin != null
    ? `${result.new_residual_arcmin}' / ${result.new_n_matched} stars` : '?';

  const REJECT_SKIP_LABELS = {
    high_reject_fraction: 'flagged, kept -- too many stars over threshold (likely a distortion-model '
      + 'misfit, not outlier cleanup)',
    low_stars: 'flagged, kept -- too few stars would remain after trimming',
    refit_failed: 'flagged, kept -- refit on trimmed set failed',
  };
  let rejectNote = '';
  if (result.n_stars_over_threshold) {
    const quadrants = Object.entries(result.reject_by_quadrant || {})
      .map(([q, n]) => `${q}:${n}`).join(', ');
    const where = quadrants ? `, at ${quadrants}` : '';
    const what = result.reject_applied
      ? 'dropped'
      : (REJECT_SKIP_LABELS[result.reject_skipped_reason] || 'flagged, kept');
    rejectNote = ` (${result.n_stars_over_threshold} star${result.n_stars_over_threshold === 1 ? '' : 's'} `
      + `over ${result.arcmin_reject_threshold}' ${what}${where})`;
  }
  container.innerHTML = `
    <div class="cal-autofit-status">Residual: ${escHtml(oldR)} → ${escHtml(newR)} `
      + `(${result.n_stars_used} stars used${escHtml(rejectNote)})</div>
    <button type="button" class="cal-autofit-accept">Accept</button>
    <button type="button" class="cal-autofit-discard">Discard</button>`;

  wireAcceptDiscard(container, host, cam, result);
}

function wireAcceptDiscard(container, host, cam, result) {
  container.querySelector('.cal-autofit-accept')?.addEventListener('click', async () => {
    const acceptBtn = container.querySelector('.cal-autofit-accept');
    const discardBtn = container.querySelector('.cal-autofit-discard');
    if (acceptBtn) acceptBtn.disabled = true;
    if (discardBtn) discardBtn.disabled = true;
    let applyResult;
    try {
      applyResult = await acceptAutofit(host, cam, result.candidate_id, result.accept_token);
    } catch (e) {
      applyResult = { status: 'error', error: 'accept request failed' };
    }
    if (applyResult.status === 'applied') {
      container.innerHTML = '<span class="cal-autofit-status cal-autofit-applied">Applied — backup saved</span>';
    } else {
      container.innerHTML = `<span class="cal-autofit-status cal-autofit-error">Apply failed: `
        + `${escHtml(applyResult.error || 'unknown error')}</span>`;
    }
  });

  container.querySelector('.cal-autofit-discard')?.addEventListener('click', () => {
    container.style.display = 'none';
    container.innerHTML = '';
  });
}

async function pollAutofitStatus(container, host, cam) {
  const maxAttempts = 90;   // ~7.5 min at 5s intervals -- generous for astrometry.net
  const intervalMs = 5000;
  for (let i = 0; i < maxAttempts; i++) {
    let result;
    try {
      result = await fetchAutofitStatus(host, cam);
    } catch (e) {
      renderAutofitResult(container, host, cam, { status: 'error', error: 'status poll failed' });
      return;
    }
    if (result.status !== 'running') {
      renderAutofitResult(container, host, cam, result);
      return;
    }
    renderAutofitResult(container, host, cam, result);
    await new Promise(resolve => setTimeout(resolve, intervalMs));
  }
  renderAutofitResult(container, host, cam, { status: 'error', error: 'timed out waiting for a result' });
}

function wireAutofitButton(card, host, cam) {
  wireCalibrationRun(card, host, cam, '.cal-autofit-btn', triggerAutofit);
  wireCalibrationRun(card, host, cam, '.cal-rebase-btn', triggerRebase);
}

function wireCalibrationRun(card, host, cam, selector, trigger) {
  const btn = card.querySelector(selector);
  const resultEl = card.querySelector('.cal-autofit-result');
  if (!btn || !resultEl) return;

  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      const triggerResult = await trigger(host, cam);
      if (!triggerResult.ok) {
        renderAutofitResult(resultEl, host, cam, { status: 'error', error: triggerResult.error || 'trigger failed' });
        btn.disabled = false;
        return;
      }
    } catch (e) {
      renderAutofitResult(resultEl, host, cam, { status: 'error', error: 'trigger request failed' });
      btn.disabled = false;
      return;
    }
    renderAutofitResult(resultEl, host, cam, { status: 'running' });
    await pollAutofitStatus(resultEl, host, cam);
    btn.disabled = false;
  });
}

async function init() {
  const grid = document.getElementById('cal-grid');
  const summaryEl = document.getElementById('cal-summary');
  wireZoomModal();

  let stations;
  try {
    stations = await (await fetch('/api/stations')).json();
  } catch (e) {
    grid.innerHTML = '<div class="cal-error">Failed to load station list.</div>';
    return;
  }

  const hosts = Object.keys(stations).sort();
  let totalCams = 0;
  const cardsByHost = {};

  for (const host of hosts) {
    const cams = (stations[host].cameras || []).filter(c => c.code);
    totalCams += cams.length;
    for (const c of cams) {
      grid.insertAdjacentHTML('beforeend', cardHtml(host, c.code, !!c.spectro));
    }
    // Only the RMS cameras get the per-camera fetches and the operator
    // buttons below; a spectroscopy card is already complete.
    cardsByHost[host] = cams.filter(c => !c.spectro).map(c => c.code);
  }
  summaryEl.textContent = `${hosts.length} stations, ${totalCams} cameras`;

  for (const host of hosts) {
    for (const cam of cardsByHost[host]) {
      const card = grid.querySelector(`.cal-card[data-host="${CSS.escape(host)}"][data-cam="${CSS.escape(cam)}"]`);
      if (!card) continue;
      wireStackZoom(card, host, cam);
      wireMaskButton(card, host, cam);
      wireAutofitButton(card, host, cam);
      setupMask(card, host, cam);
      fillPlots(card, cam);
      fillResidual(card, host, cam);
    }
  }

  // Auto-fit and mask editing are operator-initiated writes (see the
  // doctrine note above wireAutofitButton) -- only reveal them once we know
  // the viewer is an admin. authPromise is kicked off at module load (see
  // bottom of file), independent of this fetch-heavy init(), so it's very
  // likely already settled by the time every card above has been built.
  const auth = await authPromise;
  if (auth.admin) {
    grid.querySelectorAll('.cal-ops').forEach(el => { el.style.display = ''; });
  }
}

const authPromise = fetchAuthStatus().then(auth => auth || {});

document.addEventListener('DOMContentLoaded', init);
