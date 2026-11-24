/**
 * dashboard-video-modal.js — shared video modal component.
 *
 * Each page instantiates VideoModal into a bare container div.
 * The constructor injects all HTML; the caller never touches the DOM directly.
 *
 * One clip at a time: VDB, Archive, Highlights, Overview GMN, RMS detections,
 * Events. Synchronized multi-camera playback is MultiDetModal
 * (dashboard-multi-det-modal.js).
 *
 * Usage:
 *   import { VideoModal } from './dashboard-video-modal.js';
 *   const modal = new VideoModal(document.getElementById('my-container'), {
 *     trim: true, stitch: true, nav: true
 *   });
 *   modal.open({ src, title, detOffset, trimStart, trimEnd, detection, ... });
 *   modal.close();
 */

import { _modalOpen, _modalClose, _toast, state, escHtml, wireLockButton, lockClip, keepClip, wireProcessClipButton, saveDownload, saveBlob, downloadFailedText, fullResStackUrl, viewQuery, retryVideoIfPreparing, deriveStackName } from './dashboard-common.js';
import { wireHofButton } from './dashboard-hof-add.js';
import { wireBasketButton } from './dashboard-basket-add.js';
import { wireHideButton, hideTarget } from './dashboard-hidden.js';

/**
 * True for an anonymous public visitor (no logged-in session). Read off
 * state.AUTH_USER, which every page seeds from /api/auth/status BEFORE any
 * modal can open (see #648). Operator-only actions — Download clip, Crop,
 * loop/frame download, Show/Download stack, stitch — are
 * hidden when this is true so the public sees playback controls only. The
 * server already gates those actions; this only stops us SHOWING dead
 * controls. Fail-safe: defaults to treating a missing signal as anon so we
 * never surface an operator control to an unauthenticated viewer.
 */
function _vmIsAnon() {
  return !state.AUTH_USER;
}

// Sentinel label marking a full-width heading row inside the detail grid,
// rather than a label/value pair. Not a string a real field could be called.
const _INFO_HEADING = '\u0000heading';

/**
 * GMN photometric masses span grams to micrograms, so a fixed unit renders
 * either "0.00 kg" or a wall of zeroes. Pick the unit that keeps the number
 * readable and mark it as an estimate — it is derived from the light curve
 * with an assumed luminous efficiency, not measured.
 */
export function fmtMass(kg) {
  if (kg == null || !isFinite(kg) || kg <= 0) return null;
  if (kg >= 1)     return `~${kg.toFixed(2)} kg`;
  if (kg >= 1e-3)  return `~${(kg * 1e3).toFixed(1)} g`;
  if (kg >= 1e-6)  return `~${(kg * 1e6).toFixed(1)} mg`;
  return `~${(kg * 1e9).toFixed(1)} µg`;
}

/**
 * Resolve the always-available BW FF meteor-stack URL for a clip.
 *
 * When the caller passes an explicit ``stack.url`` (a color/archive stack, or
 * an already-built URL as VDB/highlights/HoF do) that wins. Otherwise, when the
 * clip has a full identity (station host-key + camera + date + filename), derive
 * the BW FF stack webp and point it at ``/stack/…`` — the tiered media route
 * that resolves the station copy, then the storage-box archive stack, then the
 * BW FF thumbnail, so it stays downloadable for archived nights too. Returns
 * null only when nothing is derivable (e.g. an external GMN clip with no
 * ROVIMEN identity), in which case the Download-stack button stays hidden.
 */
export function stackUrlFor(opts) {
  if (opts?.stack?.url) return opts.stack.url;
  const { station, camera, date, filename } = opts || {};
  if (!station || !camera || !date || !filename) return null;
  const name = deriveStackName(filename);
  if (!name) return null;
  return `/stack/${encodeURIComponent(station)}/${encodeURIComponent(camera)}` +
         `/${encodeURIComponent(date)}/${encodeURIComponent(name)}`;
}

/**
 * Resolve the URL the "Download stack" button hands out.
 *
 * The inline preview (stackUrlFor) uses the fast /stack thumbnail, but the
 * download must be the branded full-resolution image: the archive route and the
 * /fullstack route both burn the ROVIMEN/astromania overlay in on demand, so a
 * downloaded stack is never un-attributed. Prefers an explicit ``stack.url``
 * (VDB/highlights pass the gated ``/api/archive/file/…`` URL); otherwise derives
 * ``/fullstack/…`` from the clip identity rather than the thumbnail ``/stack/…``.
 * An explicit ``/stack/…`` URL (Events and the grid pass one) is mapped to its
 * ``/fullstack/…`` twin, so it never hands out the 480 px thumbnail either.
 */
export function stackDownloadUrlFor(opts) {
  if (opts?.stack?.url) return fullResStackUrl(opts.stack.url);
  const { station, camera, date, filename } = opts || {};
  if (!station || !camera || !date || !filename) return null;
  const name = deriveStackName(filename);
  if (!name) return null;
  return `/fullstack/${encodeURIComponent(station)}/${encodeURIComponent(camera)}` +
         `/${encodeURIComponent(date)}/${encodeURIComponent(name)}`;
}

/**
 * The Video DB time window for a clip: a few minutes around the clip's start
 * (plus the detection offset when known), from the chunk name's
 * ``<CAM>_<YYYYMMDD>_<HHMMSS>`` token. {} when the name has no time.
 */
export function vmClipWindow(filename, detOffset, minutes = 3) {
  const m = /_(\d{8})_(\d{2})(\d{2})(\d{2})/.exec(filename || '');
  if (!m) return {};
  const startS = (+m[2]) * 3600 + (+m[3]) * 60 + (+m[4]) + (Number(detOffset) || 0);
  // Widen outward to whole minutes so the clip's own minute is always inside.
  const fmt = min => {
    const t = ((min % 1440) + 1440) % 1440;
    return `${String(Math.floor(t / 60)).padStart(2, '0')}:${String(t % 60).padStart(2, '0')}`;
  };
  return {
    from: fmt(Math.floor((startS - minutes * 60) / 60)),
    to:   fmt(Math.ceil((startS + minutes * 60) / 60)),
  };
}

/**
 * "Open station" / "Open in Video DB" targets for a clip, or null without a
 * station, camera and night. The night is the YYYYMMDD folder date (ISO dates
 * from Highlights / Hall of Fame are normalised); ``clip=`` makes the station
 * page open this clip's player on arrival.
 */
export function vmOpenLinks({ station, camera, date, filename, detOffset } = {}) {
  const night = String(date || '').replace(/-/g, '');
  if (!station || !camera || !/^\d{8}$/.test(night)) return null;
  const clip = /^[A-Za-z0-9._-]+\.(mkv|mp4)$/.test(filename || '') ? filename : null;
  const host = encodeURIComponent(station);
  const win = vmClipWindow(clip, detOffset);
  return {
    stationUrl: `/station/${host}/rms` + viewQuery({ cam: camera, date: night, clip },
      ['cam', 'date', 'clip']),
    vdbUrl: `/station/${host}/videodb` + viewQuery(
      { cam: camera, date: night, ...win, clip },
      ['cam', 'date', 'from', 'to', 'clip']),
    // The fleet-wide Grid (Events tab) has no per-camera/station filter — it's
    // every camera's footage for this night, narrowed to this clip's window.
    gridUrl: `/events` + viewQuery(
      { tab: 'grid', date: night, gfrom: win.from, gto: win.to },
      ['tab', 'date', 'gfrom', 'gto']),
  };
}

let _vmCount = 0;

export class VideoModal {
  static _instances = new Set();

  /**
   * @param {HTMLElement} mountEl  - Container div to inject into.
   * @param {object}      config
   *   trim   {boolean} - Show draggable trim handles.
   *   stitch {boolean} - Show stitch-prev/stitch-next row (VDB only).
   *   nav    {boolean} - Show prev/next navigation buttons in header.
   */
  constructor(mountEl, config = {}) {
    this._mount  = mountEl;
    this._id     = ++_vmCount;
    this._cfg    = {
      trim:   config.trim   ?? false,
      stitch: config.stitch ?? false,
      nav:    config.nav    ?? false,
    };
    this._el            = null;   // backdrop element
    this._opts          = null;   // current open() opts
    this._trim          = null;   // live trim-state object (single mode)
    this._raf           = null;   // playhead RAF id
    this._slowActive    = false;
    this._loopActive    = false;
    this._stackVisible  = false;
    this._infoExpanded  = false;
    this._frameTime     = null;   // intended position for frame-stepping
    this._frameSeeking  = false;  // true while a frame-step seek is in flight
    // Crop state
    this._cropActive    = false;
    this._cropBox       = null;   // { x, y, w, h } normalized 0-1 in video content space
    this._cropDragStart = null;   // pointer-down start norm coord
    this._metaAbort     = null;   // aborts the loading clip's metadata listeners

    this._ensureMount();
    VideoModal._instances.add(this);
  }

  // ── Static helpers ─────────────────────────────────────────────────────────

  /** Close every open VideoModal instance. Called by _closeAllOpenModals(). */
  static closeAll() {
    for (const inst of VideoModal._instances) {
      if (inst._el?.classList.contains('open')) inst.close();
    }
  }

  // ── Public API ─────────────────────────────────────────────────────────────

  /**
   * Open the modal.
   *
   * Opts:
   *   src          {string}   Video URL.
   *   title        {string}   Header title text.
   *   detOffset    {number}   Detection time within clip (seconds). Shows marker.
   *   trimStart    {number}   Initial trim-in point (seconds).
   *   trimEnd      {number}   Initial trim-out point (seconds).
   *   duration     {number}   Known duration hint (updated from video metadata).
   *   station, camera, date, filename  — forwarded to download handler.
   *   download     {object}   { onClick: async fn }
   *   stack        {object}   { url: string } — enables "Show stack".
   *   detection    {object}   RMS detection fields for collapsible info section.
   *   witnesses    {Array}    [{cam, station, url, time, label}] — other cameras.
   *   hide         {object}   { target, hidden, page, onChange(resp) } — the
   *                           Hide / Unhide button (dashboard-hidden.js). When
   *                           absent, a clip with a camera and a detection time
   *                           still gets it; null turns it off.
   *   stitch       {object}   { hasPrev, hasNext, onPrev, onNext, onUnstitch }
   *   nav          {object}   { hasPrev, hasNext, onPrev, onNext }
   *   onClose      {function} Called when the modal closes.
   */
  open(opts) {
    // Reset stack DOM before clearing _stackVisible — _openSingle checks it too late
    if (this._stackVisible) {
      this._q('.vm-video-el').style.display     = '';
      this._q('.vm-stack-img').style.display    = 'none';
      this._q('.vm-controls-row').style.display = '';
      const _sb = this._q('.vm-stack-show-btn');
      const _bb = this._q('.vm-backvideo-btn');
      const _sd = this._q('.vm-stack-dl-btn');
      const _cd = this._q('.vm-color-stack-dl-btn');
      if (_sb) _sb.style.display = '';
      if (_bb) _bb.style.display = 'none';
      if (_sd) _sd.style.display = 'none';
      if (_cd) _cd.style.display = 'none';
    }

    this._opts          = opts;
    this._slowActive    = false;
    this._loopActive    = false;
    this._stackVisible  = false;
    this._videoDeferred = false;
    this._infoExpanded  = false;
    this._frameTime     = null;
    this._frameSeeking  = false;

    this._openSingle(opts);
    _modalOpen(this._el);
  }

  /** Close the modal, pausing all media and clearing sources. */
  close() {
    if (!this._el?.classList.contains('open')) return;
    this._stopRaf();
    this._q('.vm-actions-row')?.classList.remove('vm-sheet-open');
    this._metaAbort?.abort();
    this._metaAbort = null;

    const vid = this._q('.vm-video-el');
    if (vid) { vid.pause(); vid.src = ''; vid.load(); }

    _modalClose(this._el);
    this._opts?.onClose?.();
  }

  /** Navigate prev (dir=-1) or next (dir=1). */
  navigate(dir) {
    const cb = dir < 0 ? this._opts?.nav?.onPrev : this._opts?.nav?.onNext;
    cb?.();
  }

  /**
   * Merge partial trim-state updates from outside (e.g. after VDB stitching).
   * Call after fetching a stitched video URL to update duration, stitchAt, etc.
   */
  updateTrim(partial) {
    if (!this._trim) return;
    Object.assign(this._trim, partial);
    this._trimDraw();
    this._durLabel();
    this._syncDownloadButtons();
  }

  /**
   * Update stitch row button states.
   * Call whenever the stitch availability changes (e.g. after a stitch operation).
   * @param {object} opts  { hasPrev, hasNext, label, isStitched }
   */
  updateStitch(opts) {
    const prevBtn = this._q('.vm-stitch-prev');
    const nextBtn = this._q('.vm-stitch-next');
    const unstBtn = this._q('.vm-stitch-unstitch');
    const label   = this._q('.vm-stitch-label');
    if (!prevBtn) return;
    if (opts.isStitched) {
      prevBtn.style.display = 'none';
      nextBtn.style.display = 'none';
      unstBtn.style.display = '';
      if (label) label.textContent = opts.label || '';
    } else {
      prevBtn.style.display = '';
      nextBtn.style.display = '';
      unstBtn.style.display = 'none';
      prevBtn.disabled = !opts.hasPrev;
      nextBtn.disabled = !opts.hasNext;
      if (label) label.textContent = '';
    }
  }

  /** Returns a snapshot of current trim state (used by download handlers). */
  getTrimState() {
    return this._trim ? { ...this._trim } : null;
  }

  /** Returns the <video> element (used by VDB RAF loop for time reading). */
  getVideoEl() {
    return this._q('.vm-video-el');
  }

  // ── Mount ──────────────────────────────────────────────────────────────────

  _ensureMount() {
    this._el = document.createElement('div');
    this._el.className = 'vm-backdrop';
    this._el.setAttribute('role', 'dialog');
    this._el.setAttribute('aria-modal', 'true');
    this._el.setAttribute('aria-hidden', 'true');
    this._el.setAttribute('aria-labelledby', `vm-title-${this._id}`);
    this._el.dataset.vmId = this._id;
    this._el.innerHTML = this._html();
    this._mount.appendChild(this._el);
    this._bindShell();
  }

  _html() {
    const c = this._cfg;
    // Trim handle visibility is set via CSS display, not hidden attribute,
    // so pointer events don't fire when handles are hidden.
    const navHide = c.nav ? '' : 'display:none;';
    return `
<div class="vm-dialog">

  <div class="vm-header">
    <button type="button" class="vm-nav-btn vm-nav-prev" aria-label="Previous" style="${navHide}">&#8592;</button>
    <span class="vm-title" id="vm-title-${this._id}"></span>
    <div class="vm-header-right">
      <div class="vm-open-links" style="display:none">
        <a class="vm-open-link vm-open-station" href="#" title="Open this camera's station page on this night"><span class="vm-open-verb">Open station</span><span class="vm-open-short">Station</span></a>
        <a class="vm-open-link vm-open-vdb" href="#" title="Open this camera in the Video DB at this time"><span class="vm-open-verb">Open in </span>Video DB</a>
        <a class="vm-open-link vm-open-grid" href="#" title="Loads videos from all cameras at this time"><span class="vm-open-verb">Open in </span>Video Grid</a>
      </div>
      <button type="button" class="vm-nav-btn vm-nav-next" aria-label="Next" style="${navHide}">&#8594;</button>
      <button type="button" class="vm-close-btn" aria-label="Close" title="Close"><span aria-hidden="true">&#10005;</span></button>
    </div>
  </div>

  <div class="vm-single-section">

    <div class="vm-video-section">
      <div class="vm-video-wrap" tabindex="0" role="button" aria-label="Play or pause (Space)">
        <video class="vm-video-el" playsinline></video>
        <img class="vm-stack-img" alt="FF stack" style="display:none">
        <div class="vm-video-loading" role="status">Loading video&#x2026;</div>
        <div class="vm-video-err" style="display:none">Clip not available</div>
        <canvas class="vm-crop-canvas"></canvas>
      </div>
    </div>

    <div class="vm-controls-row">
      <div class="vm-ctrl-btns">
        <button type="button" class="vm-ctrl-btn vm-btn-play" title="Play / Pause" aria-label="Play">&#9654;</button>
        <button type="button" class="vm-ctrl-btn vm-btn-slow" title="0.5&#215; slow motion" aria-label="Slow motion" aria-pressed="false">&#189;&#215;</button>
        <button type="button" class="vm-ctrl-btn vm-btn-rewind" title="Rewind to start" aria-label="Rewind to start">&#x23EE;</button>
        <button type="button" class="vm-ctrl-btn vm-btn-rewind-det" title="Jump to detection" aria-label="Jump to detection" style="display:none">&#x23EE;&#9670;</button>
        <button type="button" class="vm-ctrl-btn vm-btn-frame-back"  title="Previous frame" aria-label="Previous frame">&#9664;&#9474;</button>
        <button type="button" class="vm-ctrl-btn vm-btn-frame-fwd"   title="Next frame" aria-label="Next frame">&#9474;&#9654;</button>
        <button type="button" class="vm-ctrl-btn vm-btn-loop" title="Loop between trim points" aria-label="Loop between trim points" aria-pressed="false">&#8635;</button>
      </div>
      <div class="vm-trackbar-wrap">
        <div class="vm-trackbar" tabindex="0" role="slider" aria-label="Seek"
             aria-valuemin="0" aria-valuemax="0" aria-valuenow="0" aria-valuetext="0.0 s">
          <div class="vm-trackbar-fill"></div>
          <div class="vm-trackbar-det-marker"></div>
          <div class="vm-trackbar-stitch-marker"></div>
          <div class="vm-trackbar-playhead"></div>
          <div class="vm-trackbar-start-handle" tabindex="0" role="slider" aria-label="Trim start"></div>
          <div class="vm-trackbar-end-handle" tabindex="0" role="slider" aria-label="Trim end"></div>
        </div>
        <div class="vm-trackbar-labels">
          <span class="vm-lbl-start"></span>
          <span class="vm-lbl-det"></span>
          <span class="vm-lbl-end"></span>
        </div>
      </div>
      <div class="vm-time-display">
        <span class="vm-time-current">0:00</span>
        <span aria-hidden="true">/</span>
        <span class="vm-time-total">0:00</span>
      </div>
    </div>

    <div class="vm-stitch-row" style="display:none">
      <button type="button" class="vm-stitch-btn vm-stitch-prev">&#8592; Stitch prev</button>
      <span class="vm-stitch-label"></span>
      <button type="button" class="vm-stitch-btn vm-stitch-unstitch" style="display:none">&#10005; Unstitch</button>
      <button type="button" class="vm-stitch-btn vm-stitch-next">Stitch next &#8594;</button>
    </div>

    <div class="vm-actions-row">
      <span class="vm-dur-label"></span>
      <button type="button" class="vm-action-btn vm-switch-multi-btn" style="display:none">All stations &#8594;</button>
      <button type="button" class="vm-action-btn vm-download-btn" style="display:none" title="The whole clip at full resolution (MP4)">&#8595; Download clip</button>
      <button type="button" class="vm-action-btn vm-more-btn" aria-expanded="false" aria-haspopup="dialog">&#8943; More</button>
      <div class="vm-act-scrim"></div>
      <div class="vm-act-sheet" aria-label="Clip actions">
        <div class="vm-act-sheet-head">
          <span>Clip actions</span>
          <button type="button" class="vm-action-btn vm-act-sheet-close" aria-label="Close actions">&#10005;</button>
        </div>
        <div class="vm-act-group" data-label="Download">
          <button type="button" class="vm-action-btn secondary vm-download-trim-btn" style="display:none" title="Only the part between the trim handles (re-encoded)">&#8595; Download trimmed</button>
          <button type="button" class="vm-action-btn vm-frame-dl-btn" style="display:none">&#8595; Download frame</button>
          <div class="vm-loop-dl-wrap" style="display:none">
            <div class="vm-loop-dl-popover" style="display:none">
              <span class="vm-loop-dl-label">Iterations</span>
              <div class="vm-loop-stepper">
                <button type="button" class="vm-loop-step-btn vm-loop-step-dec" aria-label="Fewer iterations">&#8722;</button>
                <input type="number" class="vm-loop-count" min="1" max="10" value="3" readonly aria-label="Iterations">
                <button type="button" class="vm-loop-step-btn vm-loop-step-inc" aria-label="More iterations">+</button>
              </div>
              <button type="button" class="vm-action-btn vm-loop-dl-go">&#8595; Download</button>
            </div>
            <button type="button" class="vm-action-btn vm-loop-dl-btn" aria-expanded="false">&#8593; Download loop</button>
          </div>
          <a class="vm-action-btn vm-stack-dl-btn" style="display:none" download>&#8595; Download stack</a>
          <a class="vm-action-btn secondary vm-color-stack-dl-btn" style="display:none" download>&#8595; Download color stack</a>
        </div>
        <div class="vm-act-group" data-label="View">
          <button type="button" class="vm-action-btn secondary vm-crop-btn" style="display:none">&#9701; Crop</button>
          <button type="button" class="vm-action-btn secondary vm-stack-show-btn" style="display:none">&#9632; Show stack</button>
          <button type="button" class="vm-action-btn secondary vm-backvideo-btn" style="display:none">&#9654; Back to video</button>
        </div>
        <div class="vm-act-group" data-label="Keep">
          <button type="button" class="vm-action-btn secondary vm-lock-btn" style="display:none"></button>
          <button type="button" class="vm-action-btn secondary vm-hof-btn" style="display:none"></button>
          <button type="button" class="vm-action-btn secondary vm-basket-btn" style="display:none"></button>
          <button type="button" class="vm-action-btn secondary vm-process-btn" style="display:none">Process clip</button>
        </div>
        <div class="vm-act-group" data-label="Review">
          <button type="button" class="vm-action-btn secondary vm-hide-btn" style="display:none">Hide</button>
        </div>
      </div>
    </div>

    <div class="vm-info-section" style="display:none">
      <div class="vm-info-summary">
        <span class="vm-info-summary-text"></span>
        <button type="button" class="vm-info-expand-btn" aria-expanded="false">&#9660; Expand info</button>
      </div>
      <div class="vm-info-full">
        <div class="vm-info-grid"></div>
      </div>
    </div>

    <div class="vm-witnesses-section" style="display:none">
      <div class="vm-witnesses-label">Other cameras</div>
      <div class="vm-witnesses-list"></div>
    </div>

  </div><!-- end vm-single-section -->

</div>`;
  }

  _q(sel) { return this._el?.querySelector(sel) ?? null; }

  // ── Shell event bindings ───────────────────────────────────────────────────

  _bindShell() {
    this._el.addEventListener('click', (e) => {
      if (e.target === this._el) this.close();
    });
    // Escape closes an open popover first, then the modal. The modal keeps
    // the key to itself so a dialog or sheet underneath stays open.
    this._el.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape' || !this._el.classList.contains('open')) return;
      e.preventDefault(); e.stopPropagation();
      const pop = this._q('.vm-loop-dl-popover');
      if (pop && pop.style.display !== 'none') {
        pop.style.display = 'none';
        const btn = this._q('.vm-loop-dl-btn');
        btn?.setAttribute('aria-expanded', 'false');
        btn?.focus();
        return;
      }
      const row = this._q('.vm-actions-row');
      if (row?.classList.contains('vm-sheet-open')) {
        row.classList.remove('vm-sheet-open');
        this._q('.vm-more-btn')?.focus();
        return;
      }
      this.close();
    });
    this._q('.vm-close-btn').addEventListener('click', () => this.close());
    // Phones (sheet layout): "More" opens the secondary actions as a bottom sheet.
    const row = this._q('.vm-actions-row');
    const setSheet = (open) => {
      row.classList.toggle('vm-sheet-open', open);
      this._q('.vm-more-btn').setAttribute('aria-expanded', String(open));
    };
    this._q('.vm-more-btn').addEventListener('click', () => setSheet(!row.classList.contains('vm-sheet-open')));
    this._q('.vm-act-sheet-close').addEventListener('click', () => setSheet(false));
    this._q('.vm-act-scrim').addEventListener('click', () => setSheet(false));
    this._q('.vm-act-sheet').addEventListener('click', (e) => {
      if (e.target.closest('.vm-action-btn:not(.vm-loop-dl-btn):not(.vm-loop-dl-go)')) setSheet(false);
    });

    if (this._cfg.nav) {
      this._q('.vm-nav-prev')?.addEventListener('click', () => this.navigate(-1));
      this._q('.vm-nav-next')?.addEventListener('click', () => this.navigate(1));
    }

    this._bindSingleControls();
  }

  _bindSingleControls() {
    const video = this._q('.vm-video-el');

    this._q('.vm-video-wrap').addEventListener('click', (e) => {
      // Don't fire if clicking on the error overlay
      if (e.target !== this._q('.vm-video-el') && e.target !== this._q('.vm-video-wrap')) return;
      this._togglePlay();
    });
    // Keyboard play/pause on the focused video area (Space, Enter or K).
    this._q('.vm-video-wrap').addEventListener('keydown', (e) => {
      if (e.target !== e.currentTarget) return;
      if (e.key !== ' ' && e.key !== 'Enter' && e.key !== 'k' && e.key !== 'K') return;
      e.preventDefault();
      this._togglePlay();
    });

    video.addEventListener('play',  () => {
      this._keepOnPlay();
      this._frameTime    = null;
      this._frameSeeking = false;
      this._q('.vm-video-wrap').classList.add('vm-playing');
      this._q('.vm-btn-play').innerHTML = '&#9646;&#9646;';
      this._q('.vm-btn-play').setAttribute('aria-label', 'Pause');
      this._syncFrameDlBtn(false);
    });
    video.addEventListener('pause', () => {
      this._q('.vm-video-wrap').classList.remove('vm-playing');
      this._q('.vm-btn-play').innerHTML = '&#9654;';
      this._q('.vm-btn-play').setAttribute('aria-label', 'Play');
      this._syncFrameDlBtn(true);
    });
    video.addEventListener('ended', () => {
      this._q('.vm-video-wrap').classList.remove('vm-playing');
      this._q('.vm-btn-play').innerHTML = '&#9654;';
      this._q('.vm-btn-play').setAttribute('aria-label', 'Play');
      this._syncFrameDlBtn(true);
    });
    video.addEventListener('error', () => {
      const loading = this._q('.vm-video-loading');
      const showError = () => {
        if (loading) loading.style.display = 'none';
        const err = this._q('.vm-video-err');
        if (err) err.style.display = 'flex';
        video.controls = true;
      };
      // Still being fetched from the station: keep the loading state and
      // retry when the server says, rather than "Clip not available".
      const waiting = retryVideoIfPreparing(video, {
        onWaiting: () => {
          if (!loading) return;
          loading.textContent = 'Fetching the clip from the station…';
          loading.style.display = '';
        },
        onGiveUp: showError,
      });
      if (!waiting) showError();
    });
    video.addEventListener('seeked', () => {
      if (this._frameTime === null) { this._frameSeeking = false; return; }
      // If _frameTime moved while this seek was in flight, do one more seek
      if (Math.abs(video.currentTime - this._frameTime) > 0.001) {
        video.currentTime = this._frameTime;
      } else {
        this._frameSeeking = false;
      }
    });

    this._q('.vm-btn-play').addEventListener('click',       () => this._togglePlay());
    this._q('.vm-btn-slow').addEventListener('click',       () => this._toggleSlow());
    this._q('.vm-btn-rewind').addEventListener('click',     () => this._rewind());
    this._q('.vm-btn-rewind-det').addEventListener('click', () => this._rewindToDet());
    this._q('.vm-btn-frame-back').addEventListener('click', () => this._stepFrame(-1));
    this._q('.vm-btn-frame-fwd').addEventListener('click',  () => this._stepFrame(+1));
    this._q('.vm-btn-loop').addEventListener('click',       () => this._toggleLoop());
    this._q('.vm-frame-dl-btn')?.addEventListener('click',  () => this._downloadFrame());

    this._bindTrackDrag();

    this._q('.vm-stack-show-btn')?.addEventListener('click', () => this._toggleStack());
    this._q('.vm-backvideo-btn')?.addEventListener('click',  () => this._toggleStack());
    this._q('.vm-info-expand-btn')?.addEventListener('click', () => this._toggleInfo(!this._infoExpanded));
    this._q('.vm-crop-btn')?.addEventListener('click', () => this._toggleCrop());
    this._bindCropCanvas();

    // Stop clicks inside the popover from bubbling to the outside-click handler
    this._q('.vm-loop-dl-popover')?.addEventListener('click', (e) => e.stopPropagation());
    this._q('.vm-loop-dl-btn')?.addEventListener('click', (e) => {
      e.stopPropagation();
      const pop = this._q('.vm-loop-dl-popover');
      if (pop) pop.style.display = pop.style.display === 'none' ? '' : 'none';
      e.currentTarget.setAttribute('aria-expanded', pop && pop.style.display !== 'none' ? 'true' : 'false');
    });
    this._q('.vm-loop-step-dec')?.addEventListener('click', () => {
      const inp = this._q('.vm-loop-count');
      if (inp) inp.value = Math.max(1, parseInt(inp.value, 10) - 1);
    });
    this._q('.vm-loop-step-inc')?.addEventListener('click', () => {
      const inp = this._q('.vm-loop-count');
      if (inp) inp.value = Math.min(10, parseInt(inp.value, 10) + 1);
    });
    this._q('.vm-loop-dl-go')?.addEventListener('click', () => {
      const n = Math.max(1, Math.min(10, parseInt(this._q('.vm-loop-count')?.value, 10) || 3));
      this._downloadLoopClip(n);
    });
    // Close popover when clicking outside the wrap
    document.addEventListener('click', () => {
      if (!this._el?.classList.contains('open')) return;
      const pop = this._q('.vm-loop-dl-popover');
      if (pop) pop.style.display = 'none';
    });
  }

  // ── Single-video open ──────────────────────────────────────────────────────

  /**
   * Signal for the metadata listeners of the clip about to load, aborting
   * those of the previous one. The <video> is reused, so a clip that never
   * reached its metadata (still being prepared, or skipped with prev/next)
   * would otherwise leave `once` listeners that run first on the next clip
   * and replace its trim with the whole duration.
   */
  _metaSignal() {
    this._metaAbort?.abort();
    this._metaAbort = new AbortController();
    return this._metaAbort.signal;
  }

  _openSingle(opts) {
    const video = this._q('.vm-video-el');

    // Header
    this._q('.vm-title').textContent = opts.title || '';
    if (this._cfg.nav && opts.nav) {
      this._q('.vm-nav-prev').disabled = !opts.nav.hasPrev;
      this._q('.vm-nav-next').disabled = !opts.nav.hasNext;
    }

    // Reset video state — clear both video and stack image so old content is gone immediately
    this._stopRaf();
    video.pause();
    video.src = '';
    video.load();
    const _stackImgReset = this._q('.vm-stack-img');
    if (_stackImgReset) {
      _stackImgReset.src = '';
      delete _stackImgReset.dataset.full;
    }
    const _loadingEl = this._q('.vm-video-loading');
    if (_loadingEl) {
      _loadingEl.textContent = 'Loading video…';
      _loadingEl.style.display = '';
    }
    video.controls = false;
    video.playbackRate = 1.0;
    this._q('.vm-btn-slow')?.classList.remove('vm-btn-slow-active');
    this._q('.vm-btn-loop')?.classList.remove('vm-btn-loop-active');
    this._q('.vm-video-wrap').classList.remove('vm-playing');
    this._q('.vm-btn-play').innerHTML = '&#9654;';
    this._q('.vm-video-err').style.display = 'none';

    // Trim state — initialised here, updated in loadedmetadata
    this._trim = {
      start:    opts.trimStart  ?? 0,
      end:      opts.trimEnd    ?? 20,
      duration: opts.duration   ?? 20,
      detOffset: opts.detOffset ?? null,
      stitched: false,
      stitchAt: null,
      filename2: null,
      stitchAdjIsLater: false,
      // forwarded fields used by caller download handlers
      station:  opts.station  || '',
      camera:   opts.camera   || '',
      date:     opts.date     || '',
      filename: opts.filename || '',
      videoSrc: opts.src      || '',
    };

    // Wire handles visibility based on config
    const sh = this._q('.vm-trackbar-start-handle');
    const eh = this._q('.vm-trackbar-end-handle');
    if (sh) sh.style.display = this._cfg.trim ? '' : 'none';
    if (eh) eh.style.display = this._cfg.trim ? '' : 'none';

    this._trimDraw();
    this._durLabel();
    this._startRaf();

    // Load video (skipped when opening directly in stack view — deferred to "Back to video")
    const loadingEl = _loadingEl;   // already shown in reset block above
    if (!this._videoDeferred) {
      const signal = this._metaSignal();
      video._prepTries = 0;
      video.src = opts.src || '';
      video.load();
      video.addEventListener('loadedmetadata', () => {
        const dur = video.duration || this._trim.duration;
        this._trim.duration = dur;
        if (opts.trimEnd == null) this._trim.end = dur;
        else this._trim.end = Math.min(this._trim.end, dur);
        this._trimDraw();
        this._durLabel();
        if (this._trim.start > 0) video.currentTime = this._trim.start;
        video.play().catch(() => {});
      }, { once: true, signal });
      video.addEventListener('loadeddata', () => {
        if (loadingEl) loadingEl.style.display = 'none';
      }, { once: true, signal });
    }

    // Detection marker / rewind-to-det button
    const rwdDet = this._q('.vm-btn-rewind-det');
    if (rwdDet) rwdDet.style.display = opts.detOffset != null ? '' : 'none';

    // Stitch row
    if (this._cfg.stitch && opts.stitch) {
      this._q('.vm-stitch-row').style.display = '';
      const prevBtn = this._q('.vm-stitch-prev');
      const nextBtn = this._q('.vm-stitch-next');
      const unstBtn = this._q('.vm-stitch-unstitch');
      if (prevBtn) prevBtn.onclick = () => opts.stitch.onPrev?.();
      if (nextBtn) nextBtn.onclick = () => opts.stitch.onNext?.();
      if (unstBtn) unstBtn.onclick = () => opts.stitch.onUnstitch?.();
      this.updateStitch({
        hasPrev: opts.stitch.hasPrev,
        hasNext: opts.stitch.hasNext,
        isStitched: false,
      });
    } else {
      this._q('.vm-stitch-row').style.display = 'none';
    }

    // Download buttons: "Download clip" hands out the original file when the
    // caller knows where it lives (download.originalUrl); the trim-aware cut
    // (download.onClick) is then the separate "Download trimmed". Callers
    // without an original keep the cut on the main button.
    this._wireDownloadButtons();

    // "Open station" / "Open in Video DB" whenever the clip's station, camera
    // and night are known.
    const links = vmOpenLinks({
      station: opts.station, camera: opts.camera, date: opts.date,
      filename: opts.filename, detOffset: opts.detOffset,
    });
    const linksEl = this._q('.vm-open-links');
    if (linksEl) {
      linksEl.style.display = links ? '' : 'none';
      if (links) {
        this._q('.vm-open-station').href = links.stationUrl;
        this._q('.vm-open-vdb').href = links.vdbUrl;
        this._q('.vm-open-grid').href = links.gridUrl;
      }
    }

    // Frame download / loop download — frame-dl shown only when paused (video starts playing)
    const frameDlBtn = this._q('.vm-frame-dl-btn');
    const loopWrap   = this._q('.vm-loop-dl-wrap');
    const loopPop    = this._q('.vm-loop-dl-popover');
    if (frameDlBtn) frameDlBtn.style.display = 'none';
    if (loopWrap) loopWrap.style.display = opts.loopDlPath ? '' : 'none';
    if (loopPop)  loopPop.style.display  = 'none';

    // Stack — Download stack sits in the action row whenever a stack exists
    // (not gated behind "Show stack" first); the anon gate below hides it for
    // public visitors. Show stack still toggles the inline preview.
    //
    // The primary stack is the BW FF/max-pixel meteor stack, which exists for
    // EVERY detection clip. stackUrlFor() honours an explicit opts.stack.url and
    // otherwise DERIVES it from the clip identity (via /stack/… with its
    // station→archive→FF-thumbnail fallback), so Download stack is present on
    // every detection modal — events/archive included — not only when the caller
    // happened to pass a stack URL. Stash the resolved URL so _toggleStack and
    // the anon-gate restore logic can read it back.
    const stackUrl = stackUrlFor(opts);
    const stackDownloadUrl = stackDownloadUrlFor(opts);
    this._stackUrl = stackUrl;
    // The stack VIEW fills the modal, so it swaps the thumbnail for this
    // full-res image once it has loaded (see _showFullResStack).
    this._stackFullUrl = fullResStackUrl(stackUrl);
    const showBtn  = this._q('.vm-stack-show-btn');
    const stackDl  = this._q('.vm-stack-dl-btn');
    const stackImg = this._q('.vm-stack-img');
    if (stackUrl) {
      if (stackImg) stackImg.src = stackUrl;
      if (showBtn)  showBtn.style.display = '';
      if (stackDl) {
        stackDl.style.display = '';
        // Preview uses the fast thumbnail; download hands out the branded
        // full-res stack (stackDownloadUrl), never the un-branded thumbnail.
        stackDl.href = stackDownloadUrl || stackUrl;
        stackDl.setAttribute(
          'download', decodeURIComponent((stackDownloadUrl || stackUrl).split('?')[0].split('/').pop() || 'stack'));
      }
    } else {
      if (showBtn)  showBtn.style.display = 'none';
      if (stackDl)  stackDl.style.display = 'none';
    }

    // Color stack — the processed/color composite, offered IN ADDITION to the
    // BW FF stack when the caller supplies one (opts.colorStack.url). The BW
    // stack above always works even when this is absent.
    const colorStackDl = this._q('.vm-color-stack-dl-btn');
    if (colorStackDl) {
      if (opts.colorStack?.url) {
        colorStackDl.style.display = '';
        colorStackDl.href = opts.colorStack.url;
        colorStackDl.setAttribute(
          'download',
          decodeURIComponent(opts.colorStack.url.split('?')[0].split('/').pop() || 'color_stack'));
      } else {
        colorStackDl.style.display = 'none';
      }
    }

    // Open directly in stack view — show stack image, defer video to "Back to video"
    if (opts.startInStack && stackUrl) {
      this._videoDeferred = true;
      this._stackVisible  = true;
      video.style.display = 'none';
      if (loadingEl) loadingEl.style.display     = 'none';
      if (stackImg)  stackImg.style.display       = 'block';
      this._showFullResStack(stackImg);
      if (showBtn)   showBtn.style.display        = 'none';
      if (stackDl && stackDl.href) stackDl.style.display = '';
      this._q('.vm-backvideo-btn').style.display  = '';
      this._q('.vm-controls-row').style.display   = 'none';
      for (const sel of ['.vm-download-btn', '.vm-download-trim-btn']) {
        const b = this._q(sel);
        if (b) b.style.display = 'none';
      }
      if (frameDlBtn) frameDlBtn.style.display    = 'none';
      if (loopWrap)  loopWrap.style.display       = 'none';
    }

    // Switch-to-multi button
    const switchBtn = this._q('.vm-switch-multi-btn');
    if (switchBtn) {
      if (opts.switchToMulti?.onClick) {
        switchBtn.style.display = '';
        switchBtn.textContent = opts.switchToMulti.label || 'All stations →';
        switchBtn.onclick = () => opts.switchToMulti.onClick();
      } else {
        switchBtn.style.display = 'none';
      }
    }

    // Reset crop state
    this._cropActive = false;
    this._cropBox    = null;
    const cropCanvas = this._q('.vm-crop-canvas');
    if (cropCanvas) cropCanvas.style.display = 'none';
    this._q('.vm-crop-btn')?.classList.remove('vm-crop-btn-active');
    this._cropApplyLabels(false);
    this._q('.vm-crop-btn').style.display = '';

    // Anonymous public visitors get playback only — hide every operator-only
    // action (download / crop / loop+frame download / stack download+show).
    // Runs LAST so it overrides the per-control visibility set above.
    // Server-side these actions are already gated; this just stops us
    // painting dead controls.
    this._applyAnonControlGate();

    // Add-to-Hall-of-Fame toggle — admin-only, and only for a single clip with
    // a resolvable identity. wireHofButton hides itself for non-admins / clips
    // without camera+date+filename (e.g. an external GMN clip). Runs after
    // the anon gate so it owns this button's final visibility. This is what
    // makes a clip featurable from ANY section that opens the modal.
    wireHofButton(this._q('.vm-hof-btn'), {
      camera:             opts.camera,
      date:               opts.date,
      filename:           opts.filename,
      stack:              stackUrl
                            ? decodeURIComponent(stackUrl.split('?')[0].split('/').pop())
                            : null,
      shower:             opts.detection?.shower,
      peak_magnitude:     opts.detection?.mag_apparent,
      duration_s:         opts.detection?.duration_s,
      time_utc:           opts.detection?.time_utc,
      station_label:      opts.detection?.station,
      detection_offset_s: opts.detOffset,
      // Not archived yet: lock it on its station, which re-encodes and uploads
      // it, so the Hall of Fame add can follow. Station surfaces only.
      lockForUpload: (opts.station && /\.mkv$/i.test(opts.filename || '')) ? async () => {
        const data = await lockClip(opts.station, opts.camera, opts.date, opts.filename);
        wireLockButton(this._q('.vm-lock-btn'), {
          host: opts.station, camera: opts.camera, date: opts.date, filename: opts.filename,
          locked: true, lockType: 'manual',
        });
        opts.onLocked?.(data);
      } : null,
    });

    // Add-to-basket toggle — any signed-in account, same self-gating shape
    // as wireHofButton (hides for anon sessions / clips without a full
    // identity). This is what lets a clip be collected for the one-zip
    // download from ANY section that opens the modal. getTrim is read at
    // click time, so a sub-range picked with the trim handles rides along
    // and checkout delivers only those seconds.
    wireBasketButton(this._q('.vm-basket-btn'), {
      camera:        opts.camera,
      date:          opts.date,
      filename:      opts.filename,
      stack:         stackUrl
                       ? decodeURIComponent(stackUrl.split('?')[0].split('/').pop())
                       : null,
      station_label: opts.detection?.station,
      getTrim:       () => this.getTrimState(),
    });

    // Lock toggle — lets an operator lock a clip from any section that opens
    // the modal (VDB, RMS, Events, Archive) instead of SSHing to the station.
    // wireLockButton self-gates: hides for anon / non-owning sessions and for
    // clips without a full identity, so it slots in harmlessly everywhere.
    wireLockButton(this._q('.vm-lock-btn'), {
      host:     opts.station,
      camera:   opts.camera,
      date:     opts.date,
      filename: opts.filename,
      locked:   opts.locked,
      lockType: opts.lockType,
      onLocked: opts.onLocked,
    });

    // Process-clip button — clip-scoped station action (lock if unlocked +
    // re-encode + archive upload of THIS chunk). Replaces the old whole-night
    // process action in the modal: the modal always shows a single clip, so
    // its process control is clip-scoped; whole-night reruns stay on the
    // events-page station header and the sysadmin RMS table.
    // wireProcessClipButton self-gates (admin with a full .mkv identity), so
    // it slots in harmlessly everywhere the modal opens.
    wireProcessClipButton(this._q('.vm-process-btn'), {
      host:      opts.station,
      camera:    opts.camera,
      date:      opts.date,
      filename:  opts.filename,
      processed: opts.processed,
    });

    // Hide as a false positive — any signed-in account except a magic-link
    // session (wireHideButton self-gates on state.CAN_HIDE), and only for a
    // clip that names a detection.
    const hideOpts = opts.hide !== undefined ? opts.hide
      : (opts.camera && opts.detection?.time_utc
        ? { target: hideTarget(opts.detection, opts.camera), hidden: !!opts.detection.hidden }
        : null);
    wireHideButton(this._q('.vm-hide-btn'), hideOpts);

    // Detection info section
    const infoSec = this._q('.vm-info-section');
    if (opts.detection) {
      this._renderDetection(opts.detection);
      infoSec.style.display = '';
      this._q('.vm-info-full').classList.remove('expanded');
      const expBtn = this._q('.vm-info-expand-btn');
      if (expBtn) expBtn.innerHTML = '&#9660; Expand info';
    } else {
      infoSec.style.display = 'none';
    }

    // Witnesses
    const witSec = this._q('.vm-witnesses-section');
    if (opts.witnesses?.length) {
      this._renderWitnesses(opts.witnesses);
      witSec.style.display = '';
    } else {
      witSec.style.display = 'none';
    }
  }

  /** Show / wire "Download clip" and "Download trimmed" for the open clip. */
  _wireDownloadButtons() {
    const dl = this._opts?.download || {};
    const dlBtn = this._q('.vm-download-btn');
    const trimBtn = this._q('.vm-download-trim-btn');
    if (dlBtn) dlBtn.style.display = (dl.originalUrl || dl.onClick) ? '' : 'none';
    if (trimBtn) trimBtn.style.display = (dl.originalUrl && dl.onClick) ? '' : 'none';
    this._syncDownloadButtons();
  }

  /** Label and wire the two download buttons for the current selection.
   *  Once the viewer has dragged the trim handles to part of the clip, the
   *  main button downloads that selection and the second one the whole clip;
   *  before, "Download clip" ignored a moved selection and handed out the whole
   *  20 s chunk. Untouched, the main button is the whole clip and the second
   *  one the preset window. A crop owns the main button while it is set. */
  _syncDownloadButtons() {
    if (this._cropActive && this._cropBox) return;
    const dl = this._opts?.download || {};
    const dlBtn = this._q('.vm-download-btn');
    const trimBtn = this._q('.vm-download-trim-btn');
    const sel = dl.onClick ? this._selectionSeconds() : null;
    if (dlBtn) {
      if (sel != null) {
        dlBtn.textContent = `\u2193 Download selection (${sel.toFixed(1)} s)`;
        dlBtn.title = 'The part between the trim handles';
        dlBtn.onclick = () => dl.onClick(dlBtn);
      } else {
        dlBtn.textContent = '\u2193 Download clip';
        dlBtn.title = dl.originalUrl ? 'The whole clip at full resolution (MP4)'
          : 'The part between the trim handles';
        dlBtn.onclick = dl.originalUrl ? () => this._downloadOriginal(dlBtn)
          : (dl.onClick ? () => dl.onClick(dlBtn) : null);
      }
    }
    if (trimBtn) {
      if (!(dl.originalUrl && dl.onClick)) {
        trimBtn.onclick = null;
      } else if (sel != null) {
        trimBtn.textContent = '\u2193 Download whole clip';
        trimBtn.title = 'The whole clip at full resolution (MP4)';
        trimBtn.onclick = () => this._downloadOriginal(trimBtn);
      } else {
        trimBtn.textContent = '\u2193 Download trimmed';
        trimBtn.title = 'Only the part between the trim handles (re-encoded)';
        trimBtn.onclick = () => dl.onClick(trimBtn);
      }
    }
  }

  /** Length in seconds of the viewer's trim selection when they dragged a
   *  handle to part of the clip, else null. */
  _selectionSeconds() {
    const ts = this._trim;
    if (!ts || !ts.touched) return null;
    const dur = ts.duration || 0;
    const inside = ts.start > 0.05 || (dur > 0 && ts.end < dur - 0.05);
    return inside ? Math.max(0.1, ts.end - ts.start) : null;
  }

  /** Download the whole clip (download.originalUrl, an MP4 of the original
   *  frames) under the server's name, with a toast instead of a saved error
   *  page on failure. A clip still being pulled from the station is waited
   *  for. */
  async _downloadOriginal(button) {
    const url = this._opts?.download?.originalUrl;
    if (!url) return;
    const btn = button || this._q('.vm-download-btn');
    const label = btn ? btn.innerHTML : null;
    if (btn) { btn.disabled = true; btn.textContent = 'Downloading…'; }
    try {
      const fallback = this._trim?.filename || decodeURIComponent(url.split('?')[0].split('/').pop() || 'clip');
      const resp = await saveDownload(url, fallback, {
        onWait: () => { if (btn) btn.textContent = 'Preparing…'; },
      });
      if (resp.status !== 200) _toast(downloadFailedText(resp.status), 'error');
    } catch (e) {
      _toast('Download failed', 'error');
    } finally {
      if (btn) { btn.disabled = false; btn.innerHTML = label; }
    }
  }

  /**
   * Hide operator-only single-video controls for an anonymous visitor.
   * No-op for logged-in operators — they keep the full action set. Called at
   * the end of _openSingle so it wins over the per-control visibility logic.
   */
  _applyAnonControlGate() {
    if (!_vmIsAnon()) return;
    for (const sel of [
      '.vm-download-btn',      // Download clip
      '.vm-download-trim-btn', // Download trimmed
      '.vm-open-links',        // Open station / Video DB (station pages need a login)
      '.vm-frame-dl-btn',      // Download frame
      '.vm-loop-dl-wrap',      // Download loop (+ popover)
      '.vm-crop-btn',          // Crop
      '.vm-stack-dl-btn',      // Download stack (BW FF)
      '.vm-color-stack-dl-btn',// Download color stack
      '.vm-stack-show-btn',    // Show stack
      '.vm-hof-btn',           // Add to Hall of Fame (admin-only anyway)
      '.vm-basket-btn',        // Add to basket (signed-in-only anyway)
      '.vm-lock-btn',          // Lock clip (operator-only anyway)
      '.vm-process-btn',       // Process clip (admin-only anyway)
      '.vm-hide-btn',          // Hide as a false positive (signed-in only anyway)
    ]) {
      const el = this._q(sel);
      if (el) el.style.display = 'none';
    }
    // Never leave an anon session in crop mode.
    this._cropActive = false;
    this._cropBox = null;
    const cropCanvas = this._q('.vm-crop-canvas');
    if (cropCanvas) cropCanvas.style.display = 'none';
  }

  // ── Single-video playback ──────────────────────────────────────────────────

  /** Keep the open clip once it plays (keepClip: kept on the Storage Box and
   *  locked on its station), and show the lock it now has. */
  _keepOnPlay() {
    const o = this._opts;
    if (!o) return;
    keepClip(o.station, o.camera, o.date, o.filename).then(data => {
      if (!data?.locked || this._opts !== o) return;
      wireLockButton(this._q('.vm-lock-btn'), {
        host: o.station, camera: o.camera, date: o.date, filename: o.filename,
        locked: true, lockType: data.lock_type,
      });
      o.onLocked?.(data);
    });
  }

  _togglePlay() {
    const v = this._q('.vm-video-el');
    if (!v) return;
    if (v.paused) {
      if (v.ended) v.currentTime = this._trim?.start ?? 0;
      v.play().catch(() => {});
    } else {
      v.pause();
    }
  }

  _toggleSlow() {
    const v = this._q('.vm-video-el');
    if (!v) return;
    this._slowActive = !this._slowActive;
    v.playbackRate = this._slowActive ? 0.5 : 1.0;
    const slowBtn = this._q('.vm-btn-slow');
    slowBtn?.classList.toggle('vm-btn-slow-active', this._slowActive);
    slowBtn?.setAttribute('aria-pressed', this._slowActive ? 'true' : 'false');
  }

  _rewind() {
    const v = this._q('.vm-video-el');
    if (!v) return;
    v.currentTime = 0;
    v.play().catch(() => {});
  }

  _rewindToDet() {
    const v = this._q('.vm-video-el');
    const det = this._trim?.detOffset;
    if (!v || det == null) return;
    v.currentTime = Math.max(0, det - 1);
    v.play().catch(() => {});
  }

  _stepFrame(dir) {
    const v = this._q('.vm-video-el');
    if (!v) return;
    v.pause();
    const fps = 25;
    const dur = this._trim?.duration || v.duration || 0;
    const base = this._frameTime ?? v.currentTime;
    this._frameTime = Math.max(0, Math.min(dur, base + dir / fps));
    // Only issue a seek if none is in flight; the seeked handler will
    // re-seek to the latest _frameTime if it moved while we were waiting.
    if (!this._frameSeeking) {
      this._frameSeeking = true;
      v.currentTime = this._frameTime;
    }
  }

  _syncFrameDlBtn(paused) {
    if (this._stackVisible) return;   // stack view has its own action set
    const frameDl  = this._q('.vm-frame-dl-btn');
    const loopWrap = this._q('.vm-loop-dl-wrap');
    const loopPop  = this._q('.vm-loop-dl-popover');
    const loopAvail = !!(this._opts?.loopDlPath);
    if (paused) {
      if (frameDl)  frameDl.style.display  = '';
      if (loopWrap) loopWrap.style.display = 'none';
      if (loopPop)  loopPop.style.display  = 'none';
    } else {
      if (frameDl)  frameDl.style.display  = 'none';
      if (loopWrap) loopWrap.style.display = loopAvail ? '' : 'none';
    }
  }

  _downloadFrame() {
    const v = this._q('.vm-video-el');
    if (!v || v.readyState < 2) return;
    if (this._cropActive && this._cropBox) {
      this._downloadCroppedFrame(this._frameTime ?? v.currentTime);
      return;
    }
    const t    = this._trim;
    const canvas = document.createElement('canvas');
    canvas.width  = v.videoWidth  || 1280;
    canvas.height = v.videoHeight || 720;
    canvas.getContext('2d').drawImage(v, 0, 0, canvas.width, canvas.height);
    canvas.toBlob(blob => {
      if (!blob) return;
      const filename = t?.filename || t?.videoSrc?.split('/').pop() || 'frame';
      const base     = filename.replace(/\.[^.]+$/, '');
      const sec      = (this._frameTime ?? v.currentTime).toFixed(2).replace('.', 's');
      saveBlob(blob, `${base}_${sec}.jpg`);
    }, 'image/jpeg', 0.92);
  }

  async _downloadCroppedFrame(frameTime) {
    const ts   = this._trim;
    const crop = this._cropBox;
    if (!ts || !crop) return;
    const fn   = this._cropFilename();
    if (!fn) { _toast('Cannot determine file name', 'error'); return; }
    const btn  = this._q('.vm-frame-dl-btn');
    const orig = btn?.textContent;
    if (btn) { btn.disabled = true; btn.textContent = '⏳'; }
    try {
      const params = new URLSearchParams({
        x: crop.x.toFixed(4), y: crop.y.toFixed(4),
        w: crop.w.toFixed(4), h: crop.h.toFixed(4),
        ss: frameTime.toFixed(3),
      });
      const url  = `/download_cropped_frame/${ts.station}/${ts.camera}/${ts.date}/${encodeURIComponent(fn)}?${params}`;
      const resp = await fetch(url);
      if (!resp.ok) { _toast('Frame download failed', 'error'); return; }
      const blob = await resp.blob();
      const sec  = frameTime.toFixed(2).replace('.', 's');
      saveBlob(blob, (fn.replace(/\.[^.]+$/, '') || 'crop') + `_crop_${sec}.jpg`);
    } catch (_) {
      _toast('Frame download failed', 'error');
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = orig; }
    }
  }

  _toggleLoop() {
    this._loopActive = !this._loopActive;
    const loopBtn = this._q('.vm-btn-loop');
    loopBtn?.classList.toggle('vm-btn-loop-active', this._loopActive);
    loopBtn?.setAttribute('aria-pressed', this._loopActive ? 'true' : 'false');
    // If loop just activated and video is past trim end, rewind to trim start
    if (this._loopActive) {
      const v = this._q('.vm-video-el');
      const end = this._trim?.end ?? (v?.duration ?? 0);
      if (v && v.currentTime >= end) v.currentTime = this._trim?.start ?? 0;
    }
  }

  async _downloadLoopClip(loops) {
    if (this._cropActive && this._cropBox) return this._downloadCroppedLoop(loops);
    const ts   = this._trim;
    const path = this._opts?.loopDlPath;
    if (!ts || !path) return;
    const go = this._q('.vm-loop-dl-go');
    const orig = go?.textContent;
    if (go) { go.disabled = true; go.textContent = '⏳'; }
    try {
      const p = new URLSearchParams({
        start: ts.start.toFixed(3), end: ts.end.toFixed(3), loops,
      });
      const resp = await fetch(`${path}?${p}`);
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        _toast(data.detail || 'Loop download failed', 'error');
        return;
      }
      saveBlob(await resp.blob(), ts.filename.replace(/\.[^.]+$/, '') + `_loop${loops}x.mp4`);
      const pop = this._q('.vm-loop-dl-popover');
      if (pop) pop.style.display = 'none';
    } catch (_) {
      _toast('Loop download failed', 'error');
    } finally {
      if (go) { go.disabled = false; go.textContent = orig; }
    }
  }

  // ── RAF / playhead / time display ──────────────────────────────────────────

  _startRaf() {
    if (this._raf) cancelAnimationFrame(this._raf);
    const tick = () => {
      this._rafTick();
      this._raf = requestAnimationFrame(tick);
    };
    this._raf = requestAnimationFrame(tick);
  }

  _stopRaf() {
    if (this._raf) { cancelAnimationFrame(this._raf); this._raf = null; }
  }

  _rafTick() {
    const v   = this._q('.vm-video-el');
    const ph  = this._q('.vm-trackbar-playhead');
    const cur = this._q('.vm-time-current');
    const tot = this._q('.vm-time-total');
    const dur = this._trim?.duration || (v?.duration) || 0;

    if (v && ph && dur) {
      ph.style.left = `${Math.min(100, v.currentTime / dur * 100).toFixed(2)}%`;
    }
    if (cur) cur.textContent = this._fmtTime(v?.currentTime ?? 0);
    if (tot) tot.textContent = this._fmtTime(dur);
    this._syncSeekAria(v?.currentTime ?? 0, dur);

    // Loop: when active, wrap back to trim start when we hit trim end
    if (this._loopActive && v && !v.paused) {
      const end   = this._trim?.end   ?? dur;
      const start = this._trim?.start ?? 0;
      if (v.currentTime >= end) v.currentTime = start;
    }
  }

  /** Mirror the playhead position onto the trackbar's slider semantics. */
  _syncSeekAria(t, dur) {
    const bar = this._q('.vm-trackbar');
    if (!bar) return;
    bar.setAttribute('aria-valuemax', (dur || 0).toFixed(1));
    bar.setAttribute('aria-valuenow', (t || 0).toFixed(1));
    bar.setAttribute('aria-valuetext', `${(t || 0).toFixed(1)} s of ${(dur || 0).toFixed(1)} s`);
  }

  _fmtTime(s) {
    if (!isFinite(s) || s < 0) return '0:00';
    const m = Math.floor(s / 60);
    return `${m}:${Math.floor(s % 60).toString().padStart(2, '0')}`;
  }

  // ── Track bar draw & drag ──────────────────────────────────────────────────

  _trimDraw() {
    const ts = this._trim;
    if (!ts?.duration) return;
    const pct = v => `${Math.max(0, Math.min(100, v / ts.duration * 100)).toFixed(2)}%`;

    const fill = this._q('.vm-trackbar-fill');
    if (fill) { fill.style.left = pct(ts.start); fill.style.width = pct(ts.end - ts.start); }

    const sh = this._q('.vm-trackbar-start-handle');
    const eh = this._q('.vm-trackbar-end-handle');
    if (sh) sh.style.left = pct(ts.start);
    if (eh) eh.style.left = pct(ts.end);
    for (const [h, val] of [[sh, ts.start], [eh, ts.end]]) {
      if (!h) continue;
      h.setAttribute('aria-valuemin', '0');
      h.setAttribute('aria-valuemax', ts.duration.toFixed(1));
      h.setAttribute('aria-valuenow', val.toFixed(1));
      h.setAttribute('aria-valuetext', `${val.toFixed(1)} s`);
    }

    const det = this._q('.vm-trackbar-det-marker');
    if (det) {
      const show = ts.detOffset != null && ts.detOffset >= 0 && ts.detOffset <= ts.duration;
      det.style.display = show ? 'block' : 'none';
      if (show) det.style.left = pct(ts.detOffset);
    }

    const sm = this._q('.vm-trackbar-stitch-marker');
    if (sm) {
      const show = ts.stitched && ts.stitchAt != null;
      sm.style.display = show ? 'block' : 'none';
      if (show) sm.style.left = pct(ts.stitchAt);
    }

    const ls = this._q('.vm-lbl-start');
    const ld = this._q('.vm-lbl-det');
    const le = this._q('.vm-lbl-end');
    if (ls) ls.textContent = ts.start.toFixed(1) + 's';
    if (le) le.textContent = ts.end.toFixed(1) + 's';
    if (ld) ld.textContent = ts.detOffset != null ? `◆ ${ts.detOffset.toFixed(1)}s` : '';
  }

  _durLabel() {
    const ts  = this._trim;
    const lbl = this._q('.vm-dur-label');
    if (!lbl || !ts) return;
    lbl.textContent = `${(ts.end - ts.start).toFixed(1)} s clip`;
  }

  _bindTrackDrag() {
    const bar   = this._q('.vm-trackbar');
    const video = this._q('.vm-video-el');

    // Keyboard seek: arrows step 0.5 s (Shift: 5 s), Home/End jump to the
    // trim points. Stops propagation so page-level arrow shortcuts (VDB
    // prev/next clip) do not fire while the seek bar has focus.
    bar.addEventListener('keydown', (e) => {
      if (e.target !== bar) return;
      const dur = this._trim?.duration || video.duration || 0;
      if (!dur) return;
      const step = e.shiftKey ? 5 : 0.5;
      let t = null;
      if (e.key === 'ArrowRight' || e.key === 'ArrowUp') t = video.currentTime + step;
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') t = video.currentTime - step;
      else if (e.key === 'Home') t = this._trim?.start ?? 0;
      else if (e.key === 'End') t = this._trim?.end ?? dur;
      if (t === null) return;
      e.preventDefault(); e.stopPropagation();
      video.currentTime = Math.max(0, Math.min(dur, t));
      this._rafTick();
    });

    // Press anywhere on the bar (its hit zone reaches well above and below
    // the 6 px track, see .vm-trackbar::before) to seek there, and keep
    // dragging to scrub. The handles and the playhead have their own drags.
    const seekTo = (clientX) => {
      const { left, width } = bar.getBoundingClientRect();
      video.currentTime = Math.max(0, Math.min(1, (clientX - left) / width)) * (this._trim?.duration || 0);
    };
    bar.addEventListener('pointerdown', (e) => {
      if (e.target.closest('.vm-trackbar-start-handle, .vm-trackbar-end-handle, .vm-trackbar-playhead')) return;
      e.preventDefault(); e.stopPropagation();
      seekTo(e.clientX);
      bar.setPointerCapture?.(e.pointerId);
      bar.onpointermove = (ev) => seekTo(ev.clientX);
      bar.onpointerup = bar.onpointercancel = () => {
        bar.onpointermove = null; bar.onpointerup = null; bar.onpointercancel = null;
      };
    });

    // Playhead drag
    const ph = this._q('.vm-trackbar-playhead');
    ph.addEventListener('pointerdown', (e) => {
      e.preventDefault(); e.stopPropagation();
      ph.setPointerCapture(e.pointerId);
      ph.onpointermove = (ev) => {
        const { left, width } = bar.getBoundingClientRect();
        video.currentTime = Math.max(0, Math.min(1, (ev.clientX - left) / width)) * (this._trim?.duration || 0);
      };
      ph.onpointerup = () => { ph.onpointermove = null; ph.onpointerup = null; };
    });

    if (!this._cfg.trim) return;

    // Keyboard trim: arrows move a handle 0.1 s (Shift: 1 s).
    for (const [sel, key] of [['.vm-trackbar-start-handle','start'],['.vm-trackbar-end-handle','end']]) {
      this._q(sel)?.addEventListener('keydown', (e) => {
        const ts = this._trim;
        if (!ts?.duration) return;
        const step = e.shiftKey ? 1 : 0.1;
        let d = 0;
        if (e.key === 'ArrowRight' || e.key === 'ArrowUp') d = step;
        else if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') d = -step;
        else return;
        e.preventDefault(); e.stopPropagation();
        if (key === 'start') ts.start = Math.max(0, Math.min(ts.start + d, ts.end - 0.1));
        else                  ts.end   = Math.min(ts.duration, Math.max(ts.end + d, ts.start + 0.1));
        ts.touched = true;
        this._trimDraw();
        this._durLabel();
        this._syncDownloadButtons();
      });
    }

    // Trim handle drags
    for (const [sel, key] of [['.vm-trackbar-start-handle','start'],['.vm-trackbar-end-handle','end']]) {
      const handle = this._q(sel);
      if (!handle) continue;
      handle.addEventListener('pointerdown', (e) => {
        e.preventDefault(); e.stopPropagation();
        handle.setPointerCapture(e.pointerId);
        handle.onpointermove = (ev) => {
          const { left, width } = bar.getBoundingClientRect();
          const t  = Math.max(0, Math.min(1, (ev.clientX - left) / width)) * (this._trim?.duration || 0);
          const ts = this._trim;
          if (!ts) return;
          if (key === 'start') ts.start = Math.min(t, ts.end   - 0.1);
          else                  ts.end   = Math.max(t, ts.start + 0.1);
          ts.touched = true;   // the viewer's own selection from here on
          this._trimDraw();
          this._durLabel();
          this._syncDownloadButtons();
        };
        handle.onpointerup = () => { handle.onpointermove = null; handle.onpointerup = null; };
      });
    }
  }

  // ── Stack toggle ───────────────────────────────────────────────────────────

  /** Swap the stack view's 480 px thumbnail for the full-res stack. The
   *  thumbnail stays up until the big image has loaded, and a load that lands
   *  after the modal moved on to another clip is dropped. */
  _showFullResStack(img) {
    const want = this._stackFullUrl;
    if (!img || !want || want === this._stackUrl || img.dataset.full === want) return;
    const pre = new Image();
    pre.onload = () => {
      if (this._stackFullUrl !== want) return;
      img.src = want;
      img.dataset.full = want;
    };
    pre.src = want;
  }

  _toggleStack() {
    const video     = this._q('.vm-video-el');
    const img       = this._q('.vm-stack-img');
    const showBtn   = this._q('.vm-stack-show-btn');
    const backBtn   = this._q('.vm-backvideo-btn');
    const stackDl   = this._q('.vm-stack-dl-btn');
    const ctrlRow   = this._q('.vm-controls-row');
    const actRow    = this._q('.vm-actions-row');
    const stitchRow = this._q('.vm-stitch-row');
    this._stackVisible = !this._stackVisible;
    if (this._stackVisible) {
      video.pause();
      video.style.display = 'none';
      this._stitchWasVisible = stitchRow && stitchRow.style.display !== 'none';
      if (ctrlRow)   ctrlRow.style.display   = 'none';
      if (stitchRow) stitchRow.style.display = 'none';
      if (img)       img.style.display       = 'block';
      this._showFullResStack(img);
      if (showBtn)   showBtn.style.display   = 'none';
      if (backBtn)   backBtn.style.display   = '';
      if (stackDl && stackDl.href) stackDl.style.display = '';
      // Hide video-only action buttons while in stack view
      if (actRow) {
        actRow.querySelector('.vm-frame-dl-btn').style.display  = 'none';
        actRow.querySelector('.vm-loop-dl-wrap').style.display  = 'none';
        actRow.querySelector('.vm-download-btn').style.display  = 'none';
        actRow.querySelector('.vm-download-trim-btn').style.display = 'none';
        actRow.querySelector('.vm-crop-btn').style.display = 'none';
      }
      // Deactivate crop when switching to stack
      if (this._cropActive) {
        this._cropActive = false;
        this._cropBox = null;
        this._q('.vm-crop-btn')?.classList.remove('vm-crop-btn-active');
        this._cropApplyLabels(false);
      }
    } else {
      video.style.display = '';
      if (ctrlRow)   ctrlRow.style.display   = '';
      if (stitchRow) stitchRow.style.display = this._stitchWasVisible ? '' : 'none';
      if (img)       img.style.display       = 'none';
      if (showBtn)   showBtn.style.display   = '';
      if (backBtn)   backBtn.style.display   = 'none';
      // Download stack stays in the action row after leaving stack view.
      if (stackDl)   stackDl.style.display   = this._stackUrl ? '' : 'none';
      const colorStackDl = this._q('.vm-color-stack-dl-btn');
      if (colorStackDl) colorStackDl.style.display = this._opts?.colorStack?.url ? '' : 'none';
      // Restore download-clip and crop buttons (entering the stack view
      // already switched any crop off).
      this._wireDownloadButtons();
      const cropBtn = this._q('.vm-crop-btn');
      if (cropBtn) cropBtn.style.display = '';
      if (this._videoDeferred) {
        // First time showing video — load it now
        this._videoDeferred = false;
        const loadingEl = this._q('.vm-video-loading');
        if (loadingEl) loadingEl.style.display = '';
        const signal = this._metaSignal();
        video._prepTries = 0;
      video.src = this._opts?.src || '';
        video.load();
        video.addEventListener('loadedmetadata', () => {
          const dur = video.duration || this._trim?.duration;
          if (this._trim) {
            this._trim.duration = dur;
            if (this._opts?.trimEnd == null) this._trim.end = dur;
          }
          this._trimDraw();
          this._durLabel();
          if (this._trim?.start > 0) video.currentTime = this._trim.start;
          video.play().catch(() => {});
        }, { once: true, signal });
        video.addEventListener('loadeddata', () => {
          if (loadingEl) loadingEl.style.display = 'none';
        }, { once: true, signal });
      } else {
        video.play().catch(() => {});
      }
      // play event fires → _syncFrameDlBtn(false) restores the loop button
    }
  }

  // ── Info section ───────────────────────────────────────────────────────────

  _toggleInfo(expand) {
    this._infoExpanded = expand;
    const full = this._q('.vm-info-full');
    const btn  = this._q('.vm-info-expand-btn');
    if (!full) return;
    full.classList.toggle('expanded', expand);
    if (btn) {
      btn.innerHTML = expand ? '&#9650; Collapse info' : '&#9660; Expand info';
      btn.setAttribute('aria-expanded', expand ? 'true' : 'false');
    }
    if (expand) {
      setTimeout(() => {
        const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
        this._el?.scrollTo({ top: this._el.scrollHeight, behavior: reduce ? 'auto' : 'smooth' });
      }, 260);
    }
  }

  _renderDetection(det) {
    const fmt    = (v, p = 2) => v != null ? Number(v).toFixed(p) : '—';
    const fmtMag = v => v != null ? (Number(v) >= 0 ? '+' : '') + Number(v).toFixed(1) : '—';
    const fmtRD  = (ra, dec, p = 2) => {
      if (ra == null && dec == null) return '—';
      return `${ra  != null ? fmt(ra,  p) + '°' : '—'}, ` +
             `${dec != null ? (Number(dec) >= 0 ? '+' : '') + fmt(dec, p) + '°' : '—'}`;
    };

    const shower = det.shower && det.shower !== 'SPO' ? det.shower : 'Sporadic';
    const traj = det.trajectory || null;

    // Collapsed 1-line summary
    const sumParts = [];
    if (det.mag_apparent != null) sumParts.push('Mag ' + fmtMag(det.mag_apparent));
    sumParts.push(shower);
    if (det.duration_s != null) sumParts.push(fmt(det.duration_s, 1) + 's');
    if (traj) {
      // The one fact worth carrying in the collapsed line: this meteor has a
      // real trajectory, so there is an orbit waiting behind "Expand info".
      // The entry speed, as on the cards; the geocentric Vg (which drops
      // below 11.2 km/s for slow meteoroids) is in the expanded grid.
      sumParts.push(traj.velocity_init_km_s != null
        ? 'entry ' + fmt(traj.velocity_init_km_s, 1) + ' km/s · orbit'
        : 'orbit');
    }
    this._q('.vm-info-summary-text').textContent = sumParts.join(' · ');

    const fmtTime = t => t ? t.replace('T', ' ').substring(0, 19) + ' UTC' : null;
    const stationStr = det.station && det.camera && det.station !== det.camera
      ? `${det.station} (${det.camera})`
      : (det.station || det.camera || null);

    // Full grid rows
    const rows = [
      ['Time',          fmtTime(det.time_utc)],
      ['Station',       stationStr],
      ['Magnitude',     det.mag_apparent != null
        ? fmtMag(det.mag_apparent) + ' app' + (det.mag_absolute != null ? ' / ' + fmtMag(det.mag_absolute) + ' abs' : '')
        : null],
      ['Shower',        shower],
      ['Duration',      det.duration_s != null
        ? fmt(det.duration_s) + 's'
          + (det.angular_velocity != null ? ' · ' + fmt(det.angular_velocity, 1) + '°/s' : '')
          + (det.num_segments     != null ? ' · ' + det.num_segments + ' frames @ ' + (det.fps || 25) + ' fps' : '')
        : null],
      ['GMN confirmed', det.gmn_confirmed != null ? (det.gmn_confirmed ? 'Yes ✓' : 'No') : null],
      ['RA/Dec begin',  fmtRD(det.ra_beg,  det.dec_beg) !== '—' ? fmtRD(det.ra_beg,  det.dec_beg) : null],
      ['RA/Dec end',    fmtRD(det.ra_end,  det.dec_end) !== '—' ? fmtRD(det.ra_end,  det.dec_end) : null],
      ['Az/Elev begin', det.azim_beg != null ? fmt(det.azim_beg,1) + '° / ' + fmt(det.elev_beg,1) + '°' : null],
      ['Az/Elev end',   det.azim_end != null ? fmt(det.azim_end,1) + '° / ' + fmt(det.elev_end,1) + '°' : null],
      ['Radiant',       det.ra_radiant != null
        ? 'RA ' + fmt(det.ra_radiant,1) + '°, Dec ' + (Number(det.dec_radiant) >= 0 ? '+' : '') + fmt(det.dec_radiant,1) + '°'
          + (det.radiant_elev != null ? ' · elev ' + fmt(det.radiant_elev,0) + '°' : '')
        : null],
      ['Solar lon.',    det.solar_lon != null ? fmt(det.solar_lon,3) + '°' : null],
      ...this._trajectoryRows(traj, fmt, fmtMag, fmtRD),
    ].filter(([, v]) => v != null);

    const grid = this._q('.vm-info-grid');
    if (grid) {
      grid.innerHTML = rows.map(([k, v]) => k === _INFO_HEADING
        ? `<span class="vm-info-head">${escHtml(v)}</span>`
        : `<span class="vm-info-label">${k}</span><span>${escHtml(v)}</span>`
      ).join('');
    }
  }

  /** Rows describing the multi-station GMN solution this detection fed into.
   *
   *  Empty when the meteor was seen by one camera only, or when GMN has not
   *  published the night yet (it runs 4-15 days behind), which is the normal
   *  state for anything recent — the panel simply stops after the
   *  single-station block rather than showing a wall of dashes.
   */
  _trajectoryRows(traj, fmt, fmtMag, fmtRD) {
    if (!traj) return [];

    // Velocities: what it entered at, what it averaged, and the geocentric
    // value the orbit is derived from. Any subset may be missing.
    const vel = [
      traj.velocity_init_km_s != null ? 'entry ' + fmt(traj.velocity_init_km_s, 1) : null,
      traj.velocity_avg_km_s  != null ? 'mean '  + fmt(traj.velocity_avg_km_s, 1)  : null,
      traj.velocity_geo_km_s  != null ? 'geo '   + fmt(traj.velocity_geo_km_s, 1)  : null,
    ].filter(Boolean);

    // Heights read as a path (begin → peak → end). A stage GMN did not solve
    // drops out of both the numbers and the caption rather than leaving a dash
    // in the middle of the arrow chain.
    const ht = [
      ['begin', traj.altitude_begin_km],
      ['peak',  traj.altitude_peak_km],
      ['end',   traj.altitude_end_km],
    ].filter(([, v]) => v != null);
    const htStr = ht.length
      ? ht.map(([, v]) => fmt(v, 1)).join(' → ') + ' km'
        + (ht.length > 1 ? ` (${ht.map(([k]) => k).join('/')})` : '')
      : null;

    const orbit = [
      traj.orbit_a_au != null ? 'a ' + fmt(traj.orbit_a_au, 3) + ' AU' : null,
      traj.orbit_e    != null ? 'e ' + fmt(traj.orbit_e, 3)            : null,
      traj.orbit_i_deg!= null ? 'i ' + fmt(traj.orbit_i_deg, 2) + '°'  : null,
    ].filter(Boolean);
    const angles = [
      traj.orbit_peri_deg != null ? 'ω ' + fmt(traj.orbit_peri_deg, 2) + '°' : null,
      traj.orbit_node_deg != null ? 'Ω ' + fmt(traj.orbit_node_deg, 2) + '°' : null,
    ].filter(Boolean);
    const dist = [
      traj.orbit_q_au     != null ? 'q ' + fmt(traj.orbit_q_au, 3) + ' AU'     : null,
      traj.orbit_q_aph_au != null ? 'Q ' + fmt(traj.orbit_q_aph_au, 3) + ' AU' : null,
    ].filter(Boolean);

    const stations = Array.isArray(traj.stations) && traj.stations.length
      ? traj.stations.join(', ')
      : (traj.station_count != null ? traj.station_count + ' stations' : null);

    return [
      [_INFO_HEADING, 'Multi-station solution · GMN'
        + (traj.station_count != null ? ` · ${traj.station_count} cameras` : '')],
      ['Cameras',        stations],
      ['Radiant (geo)',  traj.ra_geo_deg != null || traj.dec_geo_deg != null
        ? fmtRD(traj.ra_geo_deg, traj.dec_geo_deg) : null],
      ['Velocity',       vel.length ? vel.join(' · ') + ' km/s' : null],
      ['Heliocentric V', traj.velocity_hel_km_s != null
        ? fmt(traj.velocity_hel_km_s, 1) + ' km/s' : null],
      ['Height',         htStr],
      ['Peak mag (abs)', traj.peak_absolute_magnitude != null
        ? fmtMag(traj.peak_absolute_magnitude) : null],
      ['Mass',           fmtMass(traj.mass_kg)],
      ['Orbit',          orbit.length  ? orbit.join(' · ')  : null],
      ['Node/peri',      angles.length ? angles.join(' · ') : null],
      ['q / Q',          dist.length   ? dist.join(' · ')   : null],
      ['Tisserand',      traj.tisserand != null ? fmt(traj.tisserand, 2) : null],
      // Convergence angle is the solution's quality knob: below ~15° the
      // intersecting-planes geometry is shallow and the orbit is soft.
      ['Convergence',    traj.convergence_angle_deg != null
        ? fmt(traj.convergence_angle_deg, 1) + '°' : null],
      ['Trajectory ID',  traj.trajectory_id || null],
    ];
  }

  // ── Crop box ───────────────────────────────────────────────────────────────

  _toggleCrop() {
    this._cropActive = !this._cropActive;
    const canvas = this._q('.vm-crop-canvas');
    const btn    = this._q('.vm-crop-btn');
    if (this._cropActive) {
      canvas.style.display = 'block';
      btn?.classList.add('vm-crop-btn-active');
      this._cropBox = null;
      this._cropDraw();
    } else {
      canvas.style.display = 'none';
      btn?.classList.remove('vm-crop-btn-active');
      this._cropBox = null;
      this._cropApplyLabels(false);
    }
  }

  _cropApplyLabels(cropped) {
    const dlBtn   = this._q('.vm-download-btn');
    const frameDl = this._q('.vm-frame-dl-btn');
    const loopBtn = this._q('.vm-loop-dl-btn');
    if (dlBtn)   dlBtn.textContent   = cropped ? '⬇ Download cropped clip' : '⬇ Download clip';
    if (frameDl) frameDl.textContent = cropped ? '⬇ Download cropped frame' : '⬇ Download frame';
    if (loopBtn) loopBtn.textContent = cropped ? '⬆ Download cropped loop' : '⬆ Download loop';
    // Wire onclick for download button. A crop replaces both downloads with
    // the cropped cut; without one the original/trimmed pair comes back.
    if (dlBtn) {
      if (cropped) {
        dlBtn.onclick = () => this._downloadCropped();
        const trimBtn = this._q('.vm-download-trim-btn');
        if (trimBtn) trimBtn.style.display = 'none';
      } else {
        this._wireDownloadButtons();
      }
    }
  }

  _getVideoContentRect() {
    const v      = this._q('.vm-video-el');
    const canvas = this._q('.vm-crop-canvas');
    if (!v || !v.videoWidth || !canvas) return null;
    const cw = canvas.clientWidth;
    const ch = canvas.clientHeight;
    if (!cw || !ch) return null;
    const aspectV = v.videoWidth / v.videoHeight;
    const aspectC = cw / ch;
    let vw, vh, vx, vy;
    if (aspectV > aspectC) {
      vw = cw; vh = cw / aspectV; vx = 0; vy = (ch - vh) / 2;
    } else {
      vh = ch; vw = ch * aspectV; vy = 0; vx = (cw - vw) / 2;
    }
    return { x: vx, y: vy, w: vw, h: vh };
  }

  _cropCanvasToNorm(cx, cy) {
    const vr = this._getVideoContentRect();
    if (!vr) return null;
    return {
      x: Math.max(0, Math.min(1, (cx - vr.x) / vr.w)),
      y: Math.max(0, Math.min(1, (cy - vr.y) / vr.h)),
    };
  }

  _cropDraw() {
    const canvas = this._q('.vm-crop-canvas');
    if (!canvas) return;
    const cw = canvas.clientWidth;
    const ch = canvas.clientHeight;
    canvas.width  = cw;
    canvas.height = ch;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, cw, ch);

    const vr = this._getVideoContentRect();
    if (!vr) return;

    ctx.fillStyle = 'rgba(0,0,0,0.5)';
    ctx.fillRect(0, 0, cw, ch);

    if (this._cropBox) {
      const bx = vr.x + this._cropBox.x * vr.w;
      const by = vr.y + this._cropBox.y * vr.h;
      const bw = this._cropBox.w * vr.w;
      const bh = this._cropBox.h * vr.h;
      ctx.clearRect(bx, by, bw, bh);
      ctx.strokeStyle = '#29aaff';
      ctx.lineWidth = 2;
      ctx.strokeRect(bx + 1, by + 1, bw - 2, bh - 2);
      const hs = 8;
      ctx.fillStyle = '#29aaff';
      for (const [hx, hy] of [[bx, by], [bx + bw - hs, by], [bx, by + bh - hs], [bx + bw - hs, by + bh - hs]]) {
        ctx.fillRect(hx, hy, hs, hs);
      }
    }
  }

  _bindCropCanvas() {
    const canvas = this._q('.vm-crop-canvas');
    if (!canvas) return;
    canvas.addEventListener('pointerdown', (e) => {
      if (!this._cropActive) return;
      e.preventDefault(); e.stopPropagation();
      const rect = canvas.getBoundingClientRect();
      this._cropDragStart = this._cropCanvasToNorm(e.clientX - rect.left, e.clientY - rect.top);
      canvas.setPointerCapture(e.pointerId);
      this._cropBox = null;
      this._cropApplyLabels(false);
      this._cropDraw();
    });
    canvas.addEventListener('pointermove', (e) => {
      if (!this._cropActive || !this._cropDragStart) return;
      const rect = canvas.getBoundingClientRect();
      const end  = this._cropCanvasToNorm(e.clientX - rect.left, e.clientY - rect.top);
      if (!end) return;
      const s  = this._cropDragStart;
      const vr = this._getVideoContentRect();
      // Constrain to 16:9 — compute w from mouse, derive h from aspect ratio
      // using the actual pixel dimensions of the video content area.
      let rawW = Math.abs(end.x - s.x);
      let rawH = rawW * (vr ? vr.w / vr.h : 16 / 9) * (9 / 16);
      // Clamp so the box stays within [0,1] in both axes
      const maxH = end.y > s.y ? 1 - s.y : s.y;
      if (rawH > maxH) { rawH = maxH; rawW = rawH * (vr ? vr.h / vr.w : 9 / 16) * (16 / 9); }
      const maxW = end.x > s.x ? 1 - s.x : s.x;
      rawW = Math.min(rawW, maxW);
      rawH = rawW * (vr ? vr.w / vr.h : 16 / 9) * (9 / 16);
      this._cropBox = {
        x: end.x > s.x ? s.x : s.x - rawW,
        y: end.y > s.y ? s.y : s.y - rawH,
        w: rawW, h: rawH,
      };
      this._cropDraw();
    });
    canvas.addEventListener('pointerup', () => {
      this._cropDragStart = null;
      if (this._cropBox && this._cropBox.w > 0.02 && this._cropBox.h > 0.02) {
        this._cropApplyLabels(true);
      } else {
        this._cropBox = null;
        this._cropDraw();
      }
    });
  }

  _cropFilename() {
    const ts = this._trim;
    return ts?.filename || ts?.videoSrc?.split('/').pop() || '';
  }

  async _downloadCropped() {
    const ts   = this._trim;
    const crop = this._cropBox;
    if (!ts || !crop) return;
    const fn   = this._cropFilename();
    if (!fn) { _toast('Cannot determine file name', 'error'); return; }
    const btn  = this._q('.vm-download-btn');
    const orig = btn?.textContent;
    if (btn) { btn.disabled = true; btn.textContent = '⏳'; }
    try {
      const params = new URLSearchParams({
        x: crop.x.toFixed(4), y: crop.y.toFixed(4),
        w: crop.w.toFixed(4), h: crop.h.toFixed(4),
        ss: ts.start.toFixed(3), t: Math.max(0.1, ts.end - ts.start).toFixed(3),
      });
      const url  = `/download_cropped/${ts.station}/${ts.camera}/${ts.date}/${encodeURIComponent(fn)}?${params}`;
      const resp = await fetch(url);
      if (!resp.ok) { _toast('Crop download failed', 'error'); return; }
      saveBlob(await resp.blob(), (fn.replace(/\.[^.]+$/, '') || 'crop') + '_crop.mp4');
    } catch (_) {
      _toast('Crop download failed', 'error');
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = orig; }
    }
  }

  async _downloadCroppedLoop(loops) {
    const ts   = this._trim;
    const crop = this._cropBox;
    if (!ts || !crop) return;
    const fn   = this._cropFilename();
    if (!fn) { _toast('Cannot determine file name', 'error'); return; }
    const go   = this._q('.vm-loop-dl-go');
    const orig = go?.textContent;
    if (go) { go.disabled = true; go.textContent = '⏳'; }
    try {
      const params = new URLSearchParams({
        x: crop.x.toFixed(4), y: crop.y.toFixed(4),
        w: crop.w.toFixed(4), h: crop.h.toFixed(4),
        ss: ts.start.toFixed(3), t: Math.max(0.1, ts.end - ts.start).toFixed(3),
        loops,
      });
      const url  = `/cropped_loop_clip/${ts.station}/${ts.camera}/${ts.date}/${encodeURIComponent(fn)}?${params}`;
      const resp = await fetch(url);
      if (!resp.ok) { _toast('Crop loop download failed', 'error'); return; }
      saveBlob(await resp.blob(), (fn.replace(/\.[^.]+$/, '') || 'crop') + `_crop_loop${loops}x.mp4`);
      const pop = this._q('.vm-loop-dl-popover');
      if (pop) pop.style.display = 'none';
    } catch (_) {
      _toast('Crop loop download failed', 'error');
    } finally {
      if (go) { go.disabled = false; go.textContent = orig; }
    }
  }

  // ── Witnesses ──────────────────────────────────────────────────────────────

  _renderWitnesses(witnesses) {
    const list = this._q('.vm-witnesses-list');
    if (!list) return;
    list.innerHTML = '';
    for (const w of witnesses) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'vm-witness-btn';
      btn.textContent = '▶ ' + (w.label || w.cam);
      btn.title = [w.station, w.time].filter(Boolean).join(' · ');
      btn.addEventListener('click', () => {
        const video = this._q('.vm-video-el');
        if (!video) return;

        // Rebuild modal state for the witness clip so the trackbar, detection
        // marker, duration label and download/frame/crop handlers all track the
        // new source instead of the original clip.
        this._stopRaf();
        this._trim = {
          start:    0,
          end:      20,
          duration: 20,
          detOffset: null,
          stitched: false,
          stitchAt: null,
          filename2: null,
          stitchAdjIsLater: false,
          station:  w.station || '',
          camera:   w.cam     || '',
          date:     '',
          filename: (w.url || '').split('/').pop() || '',
          videoSrc: w.url || '',
        };

        // Reset loading/error overlays so stale states from the previous clip
        // don't leak into the witness view.
        const loadingEl = this._q('.vm-video-loading');
        if (loadingEl) loadingEl.style.display = '';
        this._q('.vm-video-err').style.display = 'none';
        video.controls = false;

        this._trimDraw();
        this._durLabel();
        this._startRaf();

        video.pause();
        const signal = this._metaSignal();
        video._prepTries = 0;
        video.src = w.url;
        video.load();
        video.addEventListener('loadedmetadata', () => {
          const dur = video.duration || this._trim.duration;
          this._trim.duration = dur;
          this._trim.end = dur;
          this._trimDraw();
          this._durLabel();
          video.play().catch(() => {});
        }, { once: true, signal });
        video.addEventListener('loadeddata', () => {
          if (loadingEl) loadingEl.style.display = 'none';
        }, { once: true, signal });

        this._q('.vm-title').textContent = w.label || w.cam;
      });
      list.appendChild(btn);
    }
  }
}
