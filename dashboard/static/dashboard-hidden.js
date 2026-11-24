/*
 * dashboard-hidden.js
 * ───────────────────
 * Hiding a detection as a false positive (bird, satellite, plane, insect).
 *
 * A hide is shared by every user (server store: dashboard/hidden_detections.py,
 * routes/hidden.py). The detection lists leave hidden detections out; a
 * "Show hidden" toggle, remembered per browser, asks the API for them with
 * show_hidden=1 and they come back marked (hidden, hidden_by, hidden_at,
 * hidden_reason). Hide and Unhide controls show only for sessions that may
 * use them (state.CAN_HIDE, from /api/auth/status can_hide: any signed-in
 * account except a magic-link session).
 *
 * One click hides, with no confirmation; the page removes the card and shows
 * a short "Hidden. Undo" notice.
 *
 * Where the controls are: every list that leaves hidden detections out has
 * the toggle in its own toolbar (mountToggle / toggleHtml), every card and
 * tile of a detection clip has Hide (hideButtonHtml), and every player of a
 * detection clip has it (VideoModal ``hide``, MultiDetModal tile ``hide``).
 * Footage lists (Video DB, Grid "All footage") never leave anything out, so
 * they have no toggle; their detection clips still carry Hide and show dimmed
 * once hidden.
 */
import { state, escHtml, _toast } from './dashboard-common.js';

export const HIDE_REASONS = ['bird', 'satellite', 'plane', 'insect', 'other'];
export const SHOW_HIDDEN_KEY = 'rovimen_show_hidden';

// Bumped on every hide or unhide in this tab, and added to list URLs, so a
// list fetched after a change never comes from the browser's HTTP cache.
let _changeSeq = 0;

/** Whether the "Show hidden" toggle is on in this browser. */
export function getShowHidden() {
  try { return localStorage.getItem(SHOW_HIDDEN_KEY) === '1'; } catch { return false; }
}

export function setShowHidden(on) {
  try {
    if (on) localStorage.setItem(SHOW_HIDDEN_KEY, '1');
    else localStorage.removeItem(SHOW_HIDDEN_KEY);
  } catch { /* storage blocked: the toggle lasts for this page only */ }
}

/** Whether this session may hide and unhide (the server enforces it too). */
export function canHide() {
  return !!(state.AUTH_USER && state.CAN_HIDE);
}

/** Whether hidden detections should be listed now: the toggle is on and this
 *  session has it (a session without the toggle never lists them, whatever
 *  an earlier session left in localStorage). */
export function showingHidden() {
  return canHide() && getShowHidden();
}

/** ``url`` with show_hidden=1 when hidden detections are shown, and the
 *  change counter after a hide in this tab. */
export function withHidden(url) {
  const params = [];
  if (showingHidden()) params.push('show_hidden=1');
  if (_changeSeq) params.push(`hv=${_changeSeq}`);
  if (!params.length) return url;
  return url + (url.includes('?') ? '&' : '?') + params.join('&');
}

/** A meteor time cut to the second with a ``T`` separator (server: second_key). */
export function secondKey(t) {
  if (!t) return null;
  const s = String(t).trim().replace(' ', 'T');
  return s.length >= 19 ? s.slice(0, 19) : null;
}

/** The request body naming one detection: its key when the item has one,
 *  else its camera and meteor time (the server resolves it). Null when the
 *  item carries neither. */
export function hideTarget(item, cam) {
  if (!item) return null;
  const camera = (cam || item.cam || item.camera || '').toUpperCase();
  if (!camera) return null;
  const rms = item.rms || {};
  const ff = item.ff_file || rms.ff_file;
  const no = item.meteor_no ?? rms.meteor_no;
  if (ff && no != null) return { cam: camera, ff_file: ff, meteor_no: no };
  const t = secondKey(item.time_utc || item.meteor_time || rms.time_utc);
  return t ? { cam: camera, time_utc: t } : null;
}

/** Same detection? Compares keys when both have one, else camera + second. */
export function sameTarget(a, b) {
  if (!a || !b || a.cam !== b.cam) return false;
  if (a.ff_file && b.ff_file) return a.ff_file === b.ff_file && Number(a.meteor_no) === Number(b.meteor_no);
  const ta = a.time_utc, tb = b.time_utc;
  return !!ta && ta === tb;
}

/** The hide fields of a server response, to put on a listed item. */
export function hiddenFields(resp) {
  return {
    hidden: !!resp?.hidden,
    hidden_by: resp?.hidden ? resp.hidden_by || null : null,
    hidden_at: resp?.hidden ? resp.hidden_at || null : null,
    hidden_reason: resp?.hidden ? resp.hidden_reason || null : null,
  };
}

/** Mark (or clear) the hide fields on an item in place; returns it. */
export function applyHidden(item, resp) {
  if (!item) return item;
  Object.assign(item, hiddenFields(resp));
  if (!item.hidden) {
    delete item.hidden; delete item.hidden_by; delete item.hidden_at; delete item.hidden_reason;
  }
  return item;
}

/** Does listed ``item`` show the detection a hide/unhide response names?
 *  By key when the item carries one, else by camera and second. */
export function itemMatches(item, resp, cam) {
  if (!item || !resp?.cam) return false;
  const c = (cam || item.cam || item.camera || '').toUpperCase();
  if (c !== String(resp.cam).toUpperCase()) return false;
  const rms = item.rms || {};
  const ff = item.ff_file || rms.ff_file;
  const no = item.meteor_no ?? rms.meteor_no;
  if (ff && resp.ff_file) return ff === resp.ff_file && Number(no) === Number(resp.meteor_no);
  const t = secondKey(item.time_utc || item.meteor_time || rms.time_utc);
  return !!t && t === secondKey(resp.time_utc);
}

/** Apply a hide/unhide response to every matching item of ``items``. */
export function markAll(items, resp, cam) {
  let n = 0;
  for (const it of items || []) {
    if (itemMatches(it, resp, cam)) { applyHidden(it, resp); n++; }
  }
  return n;
}

/** Apply a hide/unhide response to a per-night detections payload
 *  (/api/detections/<date>): its clips, flat list and event witnesses. */
export function markInPayload(payload, resp) {
  if (!payload) return 0;
  let n = 0;
  for (const st of Object.values(payload.by_station || {})) {
    for (const [cam, chunks] of Object.entries(st?.cameras || {})) n += markAll(chunks, resp, cam);
  }
  n += markAll(payload.detections, resp);
  for (const ev of payload.events || []) n += markAll(ev.witnesses, resp);
  return n;
}

/** The witnesses an event lists, and whether it is listed at all: an event
 *  whose witnesses are all hidden is hidden; one with some hidden stays with
 *  the others (the server rule, hidden_detections.apply_events). */
export function eventListing(ev, show) {
  const all = ev?.witnesses || [];
  const witnesses = show ? all : all.filter(w => !w.hidden);
  const hiddenNow = all.length - all.filter(w => !w.hidden).length;
  const total = all.length + (ev?.hidden_witnesses || 0);
  return {
    witnesses,
    listed: witnesses.length > 0,
    // Witnesses the event had before any were hidden: whether it was multi-camera.
    total,
    allHidden: all.length > 0 && hiddenNow === all.length,
  };
}

/** The items a list shows: hidden ones only when ``show`` is on. */
export function visibleItems(items, show) {
  return show ? items : items.filter(it => !it?.hidden);
}

/** POST a hide (``hide`` true) or unhide. Resolves to the server state;
 *  rejects with a readable message. */
export async function setHidden(target, hide, { reason = null, page = null } = {}) {
  if (!target) throw new Error('This clip has no detection to hide');
  const body = { ...target };
  if (reason) body.reason = reason;
  if (page) body.page = page;
  const resp = await fetch(hide ? '/api/detections/hide' : '/api/detections/unhide', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const msg = resp.status === 429 ? 'Too many hides in a row; wait a minute'
      : resp.status === 403 ? 'This session may not hide detections'
      : resp.status === 401 ? 'Session expired; please sign in again'
      : resp.status === 404 ? 'No detection found for this clip'
      : `Could not save (${resp.status})`;
    throw new Error(msg);
  }
  _changeSeq++;
  return resp.json();
}

/** Date part of an ISO time, for the "hidden by" note. */
function _day(ts) {
  return ts ? String(ts).slice(0, 10) : '';
}

/** "Hidden by operator, 2026-11-22 (bird)", HTML-escaped; '' for a visible item. */
export function hiddenNoteText(item) {
  if (!item?.hidden) return '';
  const who = item.hidden_by ? ` by ${item.hidden_by}` : '';
  const day = _day(item.hidden_at);
  const reason = item.hidden_reason ? ` (${item.hidden_reason})` : '';
  return `Hidden${who}${day ? `, ${day}` : ''}${reason}`;
}

export function hiddenNoteHtml(item) {
  const t = hiddenNoteText(item);
  return t ? `<span class="hd-note">${escHtml(t)}</span>` : '';
}

/** The small Hide / Unhide button for a card; '' when this session may not
 *  hide. ``attrs`` is extra attribute text (e.g. an onclick). */
export function hideButtonHtml(item, attrs = '') {
  if (!canHide()) return '';
  const hidden = !!item?.hidden;
  const label = hidden ? 'Unhide' : 'Hide';
  const title = hidden ? 'Show this detection in the lists again'
    : 'Hide this detection from the lists for everyone (a bird, satellite, plane...)';
  return `<button type="button" class="hd-btn" data-hd="${hidden ? 'unhide' : 'hide'}" title="${escHtml(title)}" ${attrs}>${label}</button>`;
}

/** Label of the "Show hidden" toggle. */
export function toggleLabel(count) {
  return count ? `Show hidden (${count})` : 'Show hidden';
}

/**
 * The "Show hidden" toggle button: '' unless this session may hide (anonymous
 * visitors and magic-link sessions never get it). It sits in the toolbar of
 * every list of detections; ``baseClass`` is the page's own toolbar-button
 * class (``hd-toggle`` on the station page, ``grid-btn`` on the Events tabs,
 * ``hl-chip`` on Highlights), so it looks like the buttons next to it.
 * Every toggle carries ``data-hd-toggle``, which keeps several on one page in
 * step (syncToggles).
 */
export function toggleHtml(id, count = 0, extraClass = '', baseClass = 'hd-toggle') {
  if (!canHide()) return '';
  const on = getShowHidden();
  const cls = [baseClass, extraClass].filter(Boolean).join(' ');
  return `<button type="button" id="${escHtml(id)}" class="${escHtml(cls)}" data-hd-toggle aria-pressed="${on}"
    title="List the detections people hid as false positives, marked">${toggleLabel(count)}</button>`;
}

/** Update a rendered toggle's count and pressed state. */
export function updateToggle(el, count) {
  if (!el) return;
  el.textContent = toggleLabel(count);
  el.setAttribute('aria-pressed', String(getShowHidden()));
}

/** Every toggle on the page shows the stored choice (the Events page has one
 *  per tab toolbar, and a flip on one is a flip on all). */
export function syncToggles(root = document) {
  const on = String(getShowHidden());
  root.querySelectorAll?.('[data-hd-toggle]').forEach(el => el.setAttribute('aria-pressed', on));
}

/** Wire a toggle: flips the stored choice and calls ``onChange(on)``. */
export function wireToggle(el, onChange) {
  if (!el || el.dataset.hdBound) return;
  el.dataset.hdBound = '1';
  el.addEventListener('click', () => {
    const on = !getShowHidden();
    setShowHidden(on);
    el.setAttribute('aria-pressed', String(on));
    syncToggles(el.ownerDocument);
    onChange?.(on);
  });
}

/**
 * Put a wired toggle into a toolbar slot (an element the template reserves,
 * hidden until it holds a toggle). Returns the button, or null for a session
 * that may not hide, whose slot stays hidden. ``opts``: ``count``,
 * ``extraClass``, ``baseClass`` (see toggleHtml).
 */
export function mountToggle(slot, id, onChange, opts = {}) {
  if (!slot) return null;
  slot.innerHTML = toggleHtml(id, opts.count || 0, opts.extraClass || '', opts.baseClass || 'hd-toggle');
  const el = slot.querySelector('[data-hd-toggle]');
  if (el) wireToggle(el, onChange);
  slot.hidden = !el;
  return el;
}

// ── The "Hidden. Undo" notice ────────────────────────────────────────────────

let _undoEl = null;
let _undoTimer = null;

/** Show "Hidden. Undo" for a few seconds; Undo calls ``onUndo``. A second
 *  hide replaces the notice (its undo then applies to the latest hide). */
export function showUndo(text, onUndo, ms = 7000) {
  dismissUndo();
  const el = document.createElement('div');
  el.className = 'hd-undo';
  el.setAttribute('role', 'status');
  el.innerHTML = `<span>${escHtml(text)}</span> <button type="button" class="hd-undo-btn">Undo</button>`;
  el.querySelector('button').addEventListener('click', () => {
    dismissUndo();
    onUndo?.();
  });
  document.body.appendChild(el);
  _undoEl = el;
  _undoTimer = setTimeout(dismissUndo, ms);
  return el;
}

export function dismissUndo() {
  if (_undoTimer) clearTimeout(_undoTimer);
  _undoTimer = null;
  _undoEl?.remove();
  _undoEl = null;
}

/**
 * One click on a Hide / Unhide control: posts it, calls ``apply(resp)`` so
 * the page updates its own data and re-renders, and after a hide offers an
 * Undo that posts the unhide and calls ``apply`` again. Errors become a toast.
 */
export async function toggleHide(target, hide, apply, { page = null, reason = null } = {}) {
  try {
    const resp = await setHidden(target, hide, { page, reason });
    apply?.(resp);
    if (hide) {
      showUndo('Hidden.', async () => {
        try {
          apply?.(await setHidden(target, false, { page }));
        } catch (e) { _toast(e.message, 'error'); }
      });
    }
    return resp;
  } catch (e) {
    _toast(e.message, 'error');
    return null;
  }
}

/**
 * Wire the video modal's Hide button. ``hide`` is
 * ``{target, hidden, page, onChange(resp)}``; the button is hidden when this
 * session may not hide or the clip has no detection target.
 */
export function wireHideButton(btn, hide) {
  if (!btn) return;
  const target = hide?.target;
  if (!canHide() || !target) {
    btn.style.display = 'none';
    btn.onclick = null;
    return;
  }
  let hidden = !!hide.hidden;
  const paint = () => {
    btn.textContent = hidden ? 'Unhide' : 'Hide';
    btn.title = hidden ? 'Show this detection in the lists again'
      : 'Hide this detection from the lists for everyone (H: hide and go to the next clip)';
    btn.setAttribute('aria-pressed', String(hidden));
  };
  paint();
  btn.style.display = '';
  btn.onclick = () => toggleHide(target, !hidden, (resp) => {
    hidden = !!resp?.hidden;
    paint();
    hide.onChange?.(resp);
  }, { page: hide.page || 'video' });
}
