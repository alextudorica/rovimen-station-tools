/* Browser mask editor — paint a camera's RMS mask.bmp over its night stack.
 *
 * Deliberately the same interaction model as desktop/mask_brush_editor.py,
 * because operators move between the two: circular brush, left-drag paints
 * mask, right-drag erases, wheel or [ ] resizes, 1-4 are preset radii,
 * Ctrl+Z undoes a stroke, R resets. Brush painting (not polygon vertices)
 * because the things being masked are trees, roofs and light poles, whose
 * outlines are nothing like polygons.
 *
 * Mask convention is RMS's: black (0) = excluded, white (255) = observable
 * sky. What is drawn on screen is the inverse of that -- a magenta wash over
 * the EXCLUDED area only -- because on a real camera the active region is
 * nearly the whole frame, and tinting that would tint the picture.
 *
 * The canvas is kept at the mask's own pixel resolution and CSS-scaled to
 * fit, so a stroke means the same thing regardless of window size and the
 * saved PNG needs no resampling in the browser. The station resamples to
 * whatever RMS is actually capturing at before writing, which is the only
 * size that matters.
 */

const PRESET_RADII = [8, 24, 60, 140];
const MAX_UNDO = 25;
const DEFAULT_MASK_SIZE = { w: 1280, h: 720 };

let el = null;       // cached modal element refs
let ed = null;       // active editing session, null when the modal is closed

function refs() {
  if (el) return el;
  el = {
    modal:   document.getElementById('cal-mask-modal'),
    title:   document.getElementById('cal-mask-title'),
    wrap:    document.getElementById('cal-mask-canvaswrap'),
    stack:   document.getElementById('cal-mask-stack'),
    canvas:  document.getElementById('cal-mask-canvas'),
    ring:    document.getElementById('cal-mask-ring'),
    size:    document.getElementById('cal-mask-size'),
    stats:   document.getElementById('cal-mask-stats'),
    status:  document.getElementById('cal-mask-status'),
    save:    document.getElementById('cal-mask-save'),
    undo:    document.getElementById('cal-mask-undo'),
    reset:   document.getElementById('cal-mask-reset'),
    cancel:  document.getElementById('cal-mask-cancel'),
  };
  return el;
}

/* Load an image without failing the whole editor when it 404s -- a camera
   with no mask yet, or no stack yet, is a normal state to start from. */
function loadImage(url) {
  return new Promise(resolve => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => resolve(null);
    img.src = url;
  });
}

/* Read an existing mask.bmp into the editor's bit array. Anything not fully
   dark counts as sky, matching what the station does on the way back in, so
   a round trip through the editor cannot silently reinterpret a mask. */
function maskBitsFromImage(img, w, h) {
  const bits = new Uint8Array(w * h);
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  const ctx = c.getContext('2d', { willReadFrequently: true });
  if (!ctx) return bits;
  ctx.drawImage(img, 0, 0, w, h);
  const px = ctx.getImageData(0, 0, w, h).data;
  for (let i = 0, p = 0; i < bits.length; i++, p += 4) {
    if (px[p] < 128) bits[i] = 1;
  }
  return bits;
}

function paintAll() {
  const { bits, image } = ed;
  const d = image.data;
  for (let i = 0, p = 0; i < bits.length; i++, p += 4) {
    d[p] = 255; d[p + 1] = 51; d[p + 2] = 204;
    d[p + 3] = bits[i] ? 150 : 0;
  }
  ed.ctx.putImageData(image, 0, 0);
  updateStats();
}

function updateStats() {
  let masked = 0;
  for (let i = 0; i < ed.bits.length; i++) masked += ed.bits[i];
  const pct = (100 * masked / ed.bits.length).toFixed(1);
  refs().stats.textContent = `${ed.w}x${ed.h} · ${pct}% masked` + (ed.dirty ? ' · unsaved' : '');
}

export function emptyDirty() {
  return { x0: Infinity, y0: Infinity, x1: -Infinity, y1: -Infinity };
}

/* Stamp one circular brush mark into *bits*, growing *dirty* to cover what
   it touched so the repaint can be confined to that rect -- the reason the
   desktop editor stays smooth at a 140px radius and the matplotlib one it
   replaced did not. Pure: takes its target rather than reading the module's
   session, so the geometry can be tested without a canvas. */
export function stampCircle(bits, w, h, cx, cy, radius, value, dirty) {
  const x0 = Math.max(0, Math.floor(cx - radius));
  const x1 = Math.min(w - 1, Math.ceil(cx + radius));
  const y0 = Math.max(0, Math.floor(cy - radius));
  const y1 = Math.min(h - 1, Math.ceil(cy + radius));
  const r2 = radius * radius;
  for (let y = y0; y <= y1; y++) {
    const dy = y - cy;
    for (let x = x0; x <= x1; x++) {
      const dx = x - cx;
      if (dx * dx + dy * dy > r2) continue;
      bits[y * w + x] = value;
    }
  }
  if (x1 >= x0 && y1 >= y0) {
    dirty.x0 = Math.min(dirty.x0, x0); dirty.x1 = Math.max(dirty.x1, x1);
    dirty.y0 = Math.min(dirty.y0, y0); dirty.y1 = Math.max(dirty.y1, y1);
  }
}

function flushDirty(dirty) {
  if (dirty.x1 < dirty.x0 || dirty.y1 < dirty.y0) return;
  const { bits, w } = ed;
  const bw = dirty.x1 - dirty.x0 + 1;
  const bh = dirty.y1 - dirty.y0 + 1;
  const patch = ed.ctx.createImageData(bw, bh);
  const d = patch.data;
  for (let y = 0; y < bh; y++) {
    let p = y * bw * 4;
    const row = (dirty.y0 + y) * w + dirty.x0;
    for (let x = 0; x < bw; x++, p += 4) {
      d[p] = 255; d[p + 1] = 51; d[p + 2] = 204;
      d[p + 3] = bits[row + x] ? 150 : 0;
    }
  }
  ed.ctx.putImageData(patch, dirty.x0, dirty.y0);
  // Keep the full-frame buffer in step so the next repaint-from-scratch
  // (undo, reset) starts from the truth rather than a stale copy.
  const full = ed.image.data;
  for (let y = dirty.y0; y <= dirty.y1; y++) {
    for (let x = dirty.x0; x <= dirty.x1; x++) {
      const i = y * w + x;
      full[i * 4 + 3] = bits[i] ? 150 : 0;
    }
  }
}

/* Pointer position in MASK pixels. The canvas is CSS-scaled, so this is the
   one place the two coordinate systems meet. */
function toMaskXY(e) {
  const r = refs().canvas.getBoundingClientRect();
  return {
    x: (e.clientX - r.left) * ed.w / r.width,
    y: (e.clientY - r.top) * ed.h / r.height,
  };
}

function pushUndo() {
  ed.undo.push(ed.bits.slice());
  if (ed.undo.length > MAX_UNDO) ed.undo.shift();
  refs().undo.disabled = false;
}

function setBrush(radius) {
  ed.radius = Math.max(2, Math.min(400, Math.round(radius)));
  refs().size.textContent = `${ed.radius}px`;
  refs().modal.querySelectorAll('.cal-mask-preset').forEach((btn, i) => {
    btn.setAttribute('aria-pressed', String(PRESET_RADII[i] === ed.radius));
  });
  sizeRing();
}

function sizeRing() {
  const { ring, canvas } = refs();
  const r = canvas.getBoundingClientRect();
  if (!r.width) return;
  const d = 2 * ed.radius * r.width / ed.w;
  ring.style.width = `${d}px`;
  ring.style.height = `${d}px`;
}

function moveRing(e) {
  const { ring, wrap } = refs();
  const r = wrap.getBoundingClientRect();
  ring.style.left = `${e.clientX - r.left}px`;
  ring.style.top = `${e.clientY - r.top}px`;
  ring.style.display = '';
}

function markDirty() {
  ed.dirty = true;
  refs().save.disabled = false;
  updateStats();
}

function onPointerDown(e) {
  if (!ed || e.button === 1) return;
  const { canvas } = refs();
  canvas.setPointerCapture(e.pointerId);
  pushUndo();
  ed.painting = true;
  ed.value = (e.button === 2 || e.ctrlKey) ? 0 : 1;
  ed.last = toMaskXY(e);
  const dirty = emptyDirty();
  stampCircle(ed.bits, ed.w, ed.h, ed.last.x, ed.last.y, ed.radius, ed.value, dirty);
  flushDirty(dirty);
  markDirty();
}

function onPointerMove(e) {
  if (!ed) return;
  moveRing(e);
  if (!ed.painting) return;
  const pt = toMaskXY(e);
  // Interpolate: a fast drag delivers pointer events tens of pixels apart
  // and stamping only the endpoints leaves a dotted line, not a stroke.
  const dx = pt.x - ed.last.x, dy = pt.y - ed.last.y;
  const steps = Math.max(1, Math.ceil(Math.hypot(dx, dy) / Math.max(1, ed.radius / 3)));
  const dirty = emptyDirty();
  for (let i = 1; i <= steps; i++) {
    stampCircle(ed.bits, ed.w, ed.h,
      ed.last.x + dx * i / steps, ed.last.y + dy * i / steps, ed.radius, ed.value, dirty);
  }
  flushDirty(dirty);
  ed.last = pt;
  updateStats();
}

function onPointerUp() {
  if (ed) ed.painting = false;
}

function onWheel(e) {
  if (!ed) return;
  e.preventDefault();
  setBrush(ed.radius * (e.deltaY < 0 ? 1.15 : 0.87));
}

function onKey(e) {
  if (!ed) return;
  if (e.key === 'Escape') { close(); return; }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); undo(); return; }
  // Leave browser shortcuts (Ctrl+R, Alt+1, ...) to the browser.
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === '[') { setBrush(ed.radius * 0.8); return; }
  if (e.key === ']') { setBrush(ed.radius * 1.25); return; }
  if (e.key >= '1' && e.key <= '4') { setBrush(PRESET_RADII[Number(e.key) - 1]); return; }
  if (e.key.toLowerCase() === 'r') reset();
}

function undo() {
  if (!ed || !ed.undo.length) return;
  ed.bits = ed.undo.pop();
  paintAll();
  refs().undo.disabled = ed.undo.length === 0;
  markDirty();
}

function reset() {
  if (!ed) return;
  pushUndo();
  ed.bits = ed.original.slice();
  paintAll();
  markDirty();
}

/* Serialise to the black/white PNG the station expects. Built from the bit
   array rather than the on-screen canvas so the magenta wash and its alpha
   can never leak into what gets written to the camera. */
function toPngBase64() {
  const c = document.createElement('canvas');
  c.width = ed.w; c.height = ed.h;
  const ctx = c.getContext('2d');
  const img = ctx.createImageData(ed.w, ed.h);
  const d = img.data;
  for (let i = 0, p = 0; i < ed.bits.length; i++, p += 4) {
    const v = ed.bits[i] ? 0 : 255;
    d[p] = v; d[p + 1] = v; d[p + 2] = v; d[p + 3] = 255;
  }
  ctx.putImageData(img, 0, 0);
  return c.toDataURL('image/png').split(',')[1];
}

async function save() {
  const r = refs();
  r.save.disabled = true;
  r.status.textContent = 'Saving…';
  r.status.className = 'cal-mask-status';
  // Read the status and body separately. Parsing the body inside the same
  // try as the request collapsed two very different failures into one
  // useless line: a save that never reached the dashboard, and a save the
  // dashboard answered with a non-JSON error page. The second is the one
  // that actually happens -- it is what a station running a bundle without
  // /api/mask produces -- and it was reported as "the save request failed",
  // which names the wrong thing entirely.
  let result;
  try {
    const resp = await fetch(`/api/mask/${ed.host}/${ed.cam}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ png_b64: toPngBase64() }),
    });
    try {
      result = await resp.json();
    } catch (_parseError) {
      result = {
        error: `the dashboard answered HTTP ${resp.status} without JSON`,
        detail: 'the station may not have this feature yet — it arrives with '
          + 'the next updater run',
      };
    }
  } catch (e) {
    result = { error: 'the save request never reached the dashboard (offline?)' };
  }
  if (!result || !result.ok) {
    // `detail` carries what the station actually said -- notably the HTTP
    // status when it is running a bundle that predates /api/mask, which is
    // every station for the hours between a deploy and its updater window.
    const detail = result?.detail ? ` (${result.detail})` : '';
    r.status.textContent = `Save failed: ${result?.error || 'unknown error'}${detail}`;
    r.status.className = 'cal-mask-status cal-mask-error';
    r.save.disabled = false;
    return;
  }
  r.status.textContent =
    `Saved ${result.width}x${result.height} (${result.masked_pct}% masked). Applies at the next detection pass.`;
  r.status.className = 'cal-mask-status cal-mask-ok';
  ed.dirty = false;
  ed.original = ed.bits.slice();
  updateStats();
  if (ed.onSaved) ed.onSaved();
}

// Leaving the page with unsaved strokes asks first, like Close does.
function onBeforeUnload(e) {
  if (ed && ed.dirty) { e.preventDefault(); e.returnValue = ''; }
}

// The button that opened the editor gets focus back when it closes.
let returnFocus = null;

function close() {
  if (ed && ed.dirty && !window.confirm('Discard unsaved mask changes?')) return;
  const r = refs();
  r.modal.classList.remove('open');
  r.stack.removeAttribute('src');
  document.removeEventListener('keydown', onKey);
  window.removeEventListener('resize', sizeRing);
  window.removeEventListener('beforeunload', onBeforeUnload);
  ed = null;
  returnFocus?.focus?.();
  returnFocus = null;
}

let wired = false;
function wire() {
  if (wired) return;
  wired = true;
  const r = refs();
  r.canvas.addEventListener('pointerdown', onPointerDown);
  r.canvas.addEventListener('pointermove', onPointerMove);
  r.canvas.addEventListener('pointerup', onPointerUp);
  r.canvas.addEventListener('pointerleave', () => { r.ring.style.display = 'none'; });
  r.canvas.addEventListener('wheel', onWheel, { passive: false });
  r.canvas.addEventListener('contextmenu', e => e.preventDefault());
  r.save.addEventListener('click', save);
  r.undo.addEventListener('click', undo);
  r.reset.addEventListener('click', reset);
  r.cancel.addEventListener('click', close);
  r.modal.querySelectorAll('.cal-mask-preset').forEach((btn, i) => {
    btn.addEventListener('click', () => setBrush(PRESET_RADII[i]));
  });
}

export async function openMaskEditor({ host, cam, stackUrl, maskUrl, onSaved }) {
  const r = refs();
  if (!r.modal) return;
  wire();

  r.title.textContent = `${cam} — mask`;
  r.status.textContent = 'Loading…';
  r.status.className = 'cal-mask-status';
  r.stats.textContent = '';
  r.save.disabled = true;
  r.undo.disabled = true;
  returnFocus = document.activeElement;
  r.modal.classList.add('open');
  r.cancel.focus();

  const [maskImg, stackImg] = await Promise.all([loadImage(maskUrl), loadImage(stackUrl)]);

  // Size priority: the existing mask (the file we are replacing, so its
  // geometry is already the one RMS accepted), then the night stack, then
  // the fleet-standard frame. Whatever we pick, the station resamples to the
  // camera's configured capture size before writing.
  const w = maskImg?.naturalWidth || stackImg?.naturalWidth || DEFAULT_MASK_SIZE.w;
  const h = maskImg?.naturalHeight || stackImg?.naturalHeight || DEFAULT_MASK_SIZE.h;

  r.canvas.width = w;
  r.canvas.height = h;
  r.wrap.style.aspectRatio = `${w} / ${h}`;
  if (stackImg) r.stack.src = stackUrl; else r.stack.removeAttribute('src');

  const ctx = r.canvas.getContext('2d', { willReadFrequently: true });
  const bits = maskImg ? maskBitsFromImage(maskImg, w, h) : new Uint8Array(w * h);
  ed = {
    host, cam, w, h, ctx, bits,
    original: bits.slice(),
    image: ctx.createImageData(w, h),
    undo: [], radius: PRESET_RADII[1], painting: false, value: 1,
    last: { x: 0, y: 0 }, dirty: false, onSaved,
  };
  paintAll();
  setBrush(ed.radius);
  r.status.textContent = maskImg
    ? 'Left-drag masks, right-drag clears. Wheel or [ ] resize, 1-4 presets, Ctrl+Z undo, R reset.'
    : 'No mask on this camera yet — starting from a clear frame. Left-drag masks, right-drag clears.';

  document.addEventListener('keydown', onKey);
  window.addEventListener('resize', sizeRing);
  window.addEventListener('beforeunload', onBeforeUnload);
}

export const _testables = { PRESET_RADII, MAX_UNDO };
