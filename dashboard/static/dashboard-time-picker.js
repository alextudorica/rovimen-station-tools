/* dashboard-time-picker.js — hour/minute wheels for a night's time window.
 *
 * openTimePicker() shows one picker for both ends of a range (a Start | End
 * switch on top, an hour wheel and a minute wheel below, Cancel / Set):
 *   - phones (≤ 540 px): a bottom sheet over a dimmed page;
 *   - wider screens: a ~300 px pop-up under the field that was clicked.
 * Hours run in night order, 12 → 23 → 00 → 11, like the noon-anchored time
 * slider, and each carries a dot in its sky colour (day, civil, nautical,
 * night) from the same twilight data the slider is drawn from.
 *
 * Minutes run 00-59. The wheels scroll (wheel, touch, arrow keys, Page Up/Down
 * by 5 hours or 10 minutes) and can be dragged with the mouse.
 *
 * Keyboard entry: type the hour, focus moves to the minute, type the minute
 * ("1945"). A first digit that cannot start a two-digit value (3-9 for hours,
 * 6-9 for minutes) is taken on its own. Enter on Start moves to End, Enter on
 * End applies. Backspace drops a half-typed digit, else steps back a field
 * (End hour → Start minute).
 *
 * Nothing is applied until Set: onSet(start, end) receives "HH:MM" strings
 * and the caller feeds them through its normal input path.
 *
 * The wheels are the phone path. On desktop the fields themselves take a
 * typed time (wireTimeField: one click, "2333", Enter), and ↓ opens the wheels.
 */

/**
 * A typed time as "HH:MM", or null if it is not one. Accepts "2333", "933"
 * (09:33), "23:33", "23.33", "23 33", "23" (23:00) and "9" (09:00): one or
 * two digits are the hour, three or four are H MM / HH MM.
 */
export function normalizeTypedTime(text) {
  const s = String(text ?? '').trim();
  let h, m;
  const sep = /^(\d{1,2})\s*[:.\sh]\s*(\d{1,2})$/i.exec(s);
  if (sep) { h = +sep[1]; m = +sep[2]; }
  else if (/^\d{1,4}$/.test(s)) {
    if (s.length <= 2) { h = +s; m = 0; }
    else { h = +s.slice(0, s.length - 2); m = +s.slice(-2); }
  } else return null;
  if (h > 23 || m > 59) return null;
  return `${pad(h)}:${pad(m)}`;
}

/**
 * Make a time field typeable (desktop) or a button for the wheel picker
 * (phones, ≤ 540 px: no on-screen keyboard over half the screen).
 *
 * Desktop: one click selects the field's text and brings the wheels up under
 * it, so "2333" can be typed straight away (a complete time moves the wheels)
 * or the wheels used instead. Enter, Tab or a click elsewhere commits a typed
 * time through the field's normal ``change`` path, normalised to "HH:MM"
 * first (see normalizeTypedTime); Set applies the wheels. Escape restores the
 * previous value. An entry that is not a time is put back and flagged
 * ``invalid`` briefly. ArrowDown / ArrowUp move into the wheels.
 *
 * @param {HTMLInputElement} el
 * @param {object} o
 * @param {'start'|'end'} o.which
 * @param {(which:'start'|'end', extra?:{typing?:HTMLInputElement})=>void} o.openPicker
 *        opens the page's picker; pass ``extra`` through to openTimePicker
 */
export function wireTimeField(el, { which, openPicker }) {
  const phoneMq = window.matchMedia?.('(max-width: 540px)');
  const isPhone = () => !!phoneMq?.matches;
  const sync = () => {
    el.readOnly = isPhone();
    el.setAttribute('inputmode', isPhone() ? 'none' : 'numeric');
    el.title = isPhone() ? '' : 'Type a time, e.g. 2333 · ↓ for the wheel picker';
  };
  sync();
  phoneMq?.addEventListener?.('change', sync);

  let before = el.value;
  let selectOnUp = false;
  el.addEventListener('focus', () => {
    before = el.value;
    if (!el.readOnly) { el.select(); selectOnUp = true; }
  });
  // The mouseup of the focusing click would collapse the selection.
  el.addEventListener('mouseup', (e) => {
    if (selectOnUp) { e.preventDefault(); selectOnUp = false; }
  });
  el.addEventListener('click', (e) => {
    if (el.readOnly) {
      e.preventDefault();
      openPicker(which);
      return;
    }
    // Desktop: the wheels come up under the field while it keeps the caret.
    if (!timePickerOpenFor(el)) openPicker(which, { typing: el });
  });
  // Leaving the field for anything but the wheels closes them.
  el.addEventListener('blur', () => {
    setTimeout(() => {
      if (timePickerOpenFor(el) && !document.activeElement?.closest?.('.tp-panel')) {
        closeTimePicker();
      }
    }, 0);
  });
  el.addEventListener('contextmenu', (e) => { if (el.readOnly) e.preventDefault(); });
  el.addEventListener('keydown', (e) => {
    if (el.readOnly) return;
    if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      openPicker(which);   // into the wheels
    } else if (e.key === 'Escape') {
      el.value = before;
      closeTimePicker();
      el.blur();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      closeTimePicker();
      el.blur();   // commits through 'change'
    }
  });
  // Capture: runs before the page's own change handler, which then only ever
  // sees "HH:MM".
  el.addEventListener('change', (e) => {
    const norm = normalizeTypedTime(el.value);
    if (norm === null) {
      e.stopImmediatePropagation();
      el.value = before;
      el.classList.add('invalid');
      setTimeout(() => el.classList.remove('invalid'), 1200);
      return;
    }
    el.value = norm;
    el.classList.remove('invalid');
    before = norm;
  }, { capture: true });
}

const ITEM_H = 44;
const HOURS = Array.from({ length: 24 }, (_, k) => (12 + k) % 24);
const MINUTES = Array.from({ length: 60 }, (_, k) => k);
const pad = (n) => String(n).padStart(2, '0');

let openState = null;

function parseHHMM(v) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(v || '').trim());
  if (!m) return { h: 12, m: 0 };
  return { h: Math.min(23, +m[1]), m: Math.min(59, +m[2]) };
}

function buildWheel(kind, values, skyBand) {
  const wheel = document.createElement('div');
  wheel.className = `tp-wheel tp-wheel-${kind}`;
  wheel.tabIndex = 0;
  wheel.setAttribute('role', 'spinbutton');
  wheel.setAttribute('aria-label', kind === 'h' ? 'Hour (UTC)' : 'Minute');
  wheel.innerHTML = values.map((v) => {
    const band = kind === 'h' && skyBand ? skyBand(v) : null;
    const dot = band ? `<span class="tp-dot tp-sky-${band}" aria-hidden="true"></span>` : '';
    return `<div class="tp-item" data-v="${v}">${dot}${pad(v)}</div>`;
  }).join('');
  return wheel;
}

function wheelIndex(wheel) {
  return Math.max(0, Math.min(wheel.children.length - 1, Math.round(wheel.scrollTop / ITEM_H)));
}

function scrollWheelTo(wheel, idx, smooth) {
  const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  wheel.scrollTo({ top: idx * ITEM_H, behavior: smooth && !reduce ? 'smooth' : 'auto' });
}

function markSelected(wheel) {
  const idx = wheelIndex(wheel);
  [...wheel.children].forEach((el, i) => el.classList.toggle('sel', i === idx));
  const v = +wheel.children[idx].dataset.v;
  wheel.setAttribute('aria-valuenow', String(v));
  wheel.setAttribute('aria-valuetext', pad(v));
  return v;
}

export function closeTimePicker() {
  if (!openState) return;
  const { root, anchor, onKey, onDocDown, onResize, typing, onTyped } = openState;
  document.removeEventListener('keydown', onKey, true);
  document.removeEventListener('pointerdown', onDocDown, true);
  window.removeEventListener('resize', onResize);
  typing?.removeEventListener('input', onTyped);
  root.remove();
  document.documentElement.classList.remove('tp-sheet-open');
  openState = null;
  // A field being typed into keeps (or has already given away) the focus.
  if (!typing) anchor?.focus?.({ preventScroll: true });
}

/**
 * @param {object}   o
 * @param {Element}  o.anchor     the field that was clicked (pop-up position, focus return)
 * @param {string}   o.start      "HH:MM"
 * @param {string}   o.end        "HH:MM"
 * @param {'start'|'end'} o.focus which end to edit first
 * @param {(h:number)=>('day'|'civ'|'naut'|'night'|null)} [o.skyBand]
 * @param {(start:string, end:string)=>void} o.onSet
 * @param {HTMLInputElement} [o.typing] the field stays focused for typing; a
 *        complete time typed into it ("2333", "23:33") moves the wheels
 */
export function openTimePicker({ anchor, start, end, focus = 'start', skyBand = null, onSet, typing = null, limits = null }) {
  closeTimePicker();
  const sheet = window.matchMedia?.('(max-width: 540px)').matches;
  // limits: the RMS capture window as {lo, hi} in the sliders' noon-anchored
  // minutes (0 = 12:00 UTC, 1440 = 12:00 next day). Only hours inside it are
  // offered, and every value is pulled into it.
  const toN = (v) => ((v.h * 60 + v.m) - 720 + 1440) % 1440;
  const fromN = (n) => { const u = (n + 720) % 1440; return { h: Math.floor(u / 60), m: u % 60 }; };
  const clampV = (v) => {
    if (!limits) return v;
    const n = toN(v);
    const c = Math.min(limits.hi, Math.max(limits.lo, n));
    return c === n ? v : fromN(c);
  };
  const hours = limits
    ? HOURS.filter((h) => { const n0 = toN({ h, m: 0 }); return n0 + 59 >= limits.lo && n0 <= limits.hi; })
    : HOURS;
  const values = { start: clampV(parseHHMM(start)), end: clampV(parseHHMM(end)) };
  let side = focus === 'end' ? 'end' : 'start';

  const root = document.createElement('div');
  root.className = `tp-root ${sheet ? 'tp-is-sheet' : 'tp-is-pop'}`;
  root.innerHTML = `
    ${sheet ? '<div class="tp-backdrop"></div>' : ''}
    <section class="tp-panel" role="dialog" aria-modal="${sheet}" aria-label="Time range (UTC)">
      ${sheet ? '<div class="tp-grab" aria-hidden="true"></div>' : ''}
      <div class="tp-seg" role="tablist">
        <button type="button" role="tab" class="tp-side" data-side="start">Start (UTC)<b></b></button>
        <button type="button" role="tab" class="tp-side" data-side="end">End (UTC)<b></b></button>
      </div>
      <div class="tp-wheels">
        <div class="tp-band" aria-hidden="true"></div>
        <span class="tp-colon" aria-hidden="true">:</span>
      </div>
      <div class="tp-labels" aria-hidden="true"><span>Hour</span><span>Minute</span></div>
      <div class="tp-actions">
        <button type="button" class="tp-cancel">Cancel</button>
        <button type="button" class="tp-set">Set</button>
      </div>
    </section>`;
  const panel = root.querySelector('.tp-panel');
  const wheelsEl = root.querySelector('.tp-wheels');
  const hWheel = buildWheel('h', hours, skyBand);
  const mWheel = buildWheel('m', MINUTES, null);
  wheelsEl.insertBefore(hWheel, wheelsEl.querySelector('.tp-colon'));
  wheelsEl.appendChild(mWheel);
  document.body.appendChild(root);
  if (sheet) document.documentElement.classList.add('tp-sheet-open');

  const sideBtns = [...root.querySelectorAll('.tp-side')];
  const paintSides = () => sideBtns.forEach((b) => {
    const v = values[b.dataset.side];
    b.querySelector('b').textContent = `${pad(v.h)}:${pad(v.m)}`;
    const on = b.dataset.side === side;
    b.classList.toggle('on', on);
    b.setAttribute('aria-selected', String(on));
  });
  const loadSide = (smooth) => {
    scrollWheelTo(hWheel, hours.indexOf(values[side].h), smooth);
    scrollWheelTo(mWheel, MINUTES.indexOf(values[side].m), smooth);
    paintSides();
  };

  // Wheels: scroll-snap does the snapping; we only read where they settled.
  let raf = 0;
  const onScroll = () => {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => {
      values[side] = clampV({ h: markSelected(hWheel), m: markSelected(mWheel) });
      paintSides();
    });
  };
  // A wheel left outside the capture window (15:20 when RMS starts 15:39)
  // turns back to the limit once it stops.
  const onScrollEnd = () => {
    if (!limits || hWheel.classList.contains('tp-dragging') || mWheel.classList.contains('tp-dragging')) return;
    // read the wheels themselves: values[side] is refreshed a frame later
    const v = { h: hours[wheelIndex(hWheel)], m: MINUTES[wheelIndex(mWheel)] };
    const c = clampV(v);
    values[side] = c;
    paintSides();
    if (c !== v) loadSide(true);
  };
  for (const w of [hWheel, mWheel]) {
    w.addEventListener('scroll', onScroll, { passive: true });
    w.addEventListener('scrollend', onScrollEnd);
  }
  for (const w of [hWheel, mWheel]) {
    // Mouse: drag the wheel like a touch screen. Capture starts only once the
    // pointer has moved, so a plain click still lands on the item under it.
    let drag = null;
    w.addEventListener('pointerdown', (e) => {
      pending = '';
      if (e.pointerType !== 'mouse' || e.button !== 0) return;
      drag = { id: e.pointerId, y: e.clientY, top: w.scrollTop, moved: false };
    });
    w.addEventListener('pointermove', (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      const dy = e.clientY - drag.y;
      if (!drag.moved && Math.abs(dy) < 4) return;
      if (!drag.moved) {
        drag.moved = true;
        w.setPointerCapture(e.pointerId);
        w.classList.add('tp-dragging');   // snapping off while the mouse holds it
      }
      w.scrollTop = drag.top - dy;
    });
    const endDrag = (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      const moved = drag.moved;
      drag = null;
      if (!moved) return;
      w.classList.remove('tp-dragging');
      scrollWheelTo(w, wheelIndex(w), true);
      // swallow the click that follows a drag
      w.addEventListener('click', (ev) => ev.stopPropagation(), { capture: true, once: true });
    };
    w.addEventListener('pointerup', endDrag);
    w.addEventListener('pointercancel', endDrag);
    w.addEventListener('click', (e) => {
      const item = e.target.closest('.tp-item');
      if (item) scrollWheelTo(w, [...w.children].indexOf(item), true);
    });
    w.addEventListener('keydown', (e) => {
      const page = w === mWheel ? 10 : 5;
      const step = { ArrowUp: -1, ArrowDown: 1, PageUp: -page, PageDown: page }[e.key];
      if (!step) return;
      e.preventDefault();
      pending = '';
      scrollWheelTo(w, Math.max(0, Math.min(w.children.length - 1, wheelIndex(w) + step)), true);
    });
  }
  sideBtns.forEach((b) => b.addEventListener('click', () => {
    pending = '';
    if (b.dataset.side === side) return;
    side = b.dataset.side;
    loadSide(false);
  }));

  // ── Keyboard entry ──
  let pending = '';   // first digit of a two-digit value, while waiting for the second
  const goTo = (w) => { pending = ''; w.focus({ preventScroll: true }); };
  const put = (w, v) => {
    const kind = w === hWheel ? 'h' : 'm';
    values[side] = { ...values[side], [kind]: v };
    scrollWheelTo(w, (kind === 'h' ? hours : MINUTES).indexOf(v), false);
    paintSides();
  };
  // A complete time is pulled into the capture window.
  const settle = () => {
    const c = clampV(values[side]);
    if (c !== values[side]) { values[side] = c; loadSide(false); }
  };
  const typeDigit = (w, d) => {
    const max = w === hWheel ? 23 : 59;
    const fits = (v) => v <= max && (w !== hWheel || hours.includes(v));
    if (pending) {
      const v = +(pending + d);
      if (!fits(v)) return;             // e.g. "27", or an hour outside the window
      pending = '';
      put(w, v);
      if (w === hWheel) goTo(mWheel); else settle();
      return;
    }
    const canPair = d * 10 <= max && (w !== hWheel || hours.some((h) => Math.floor(h / 10) === d));
    if (fits(d)) put(w, d);
    else if (!canPair) return;           // neither an hour of the window nor its first digit
    if (canPair) { pending = String(d); return; }   // could still be 1x / 4x
    if (w === hWheel) goTo(mWheel); else settle();
  };
  const stepBack = (w) => {
    if (pending) { pending = ''; return; }
    if (w === mWheel) { goTo(hWheel); return; }
    if (side === 'end') { side = 'start'; loadSide(false); goTo(mWheel); }
  };

  const apply = () => {
    values.start = clampV(values.start);
    values.end = clampV(values.end);
    const s = `${pad(values.start.h)}:${pad(values.start.m)}`;
    const e = `${pad(values.end.h)}:${pad(values.end.m)}`;
    closeTimePicker();
    onSet?.(s, e);
  };
  root.querySelector('.tp-set').addEventListener('click', apply);
  root.querySelector('.tp-cancel').addEventListener('click', closeTimePicker);
  root.querySelector('.tp-backdrop')?.addEventListener('click', closeTimePicker);

  const onKey = (e) => {
    const w = e.target === hWheel || e.target === mWheel ? e.target
      : e.target.closest?.('.tp-side') ? hWheel : null;
    if (e.key === 'Escape') { e.preventDefault(); closeTimePicker(); }
    else if (e.key === 'Enter' && e.target.closest?.('.tp-set')) { e.preventDefault(); apply(); }
    else if (e.key === 'Enter' && (e.target === hWheel || e.target === mWheel)) {
      e.preventDefault();
      if (side === 'start') { side = 'end'; loadSide(false); goTo(hWheel); }
      else apply();
    }
    else if (w && /^[0-9]$/.test(e.key) && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault();
      typeDigit(w, +e.key);
    }
    else if (w && e.key === 'Backspace') { e.preventDefault(); stepBack(w); }
  };
  // Pop-up: a click anywhere else cancels, like any other menu.
  const onDocDown = (e) => {
    if (!sheet && !panel.contains(e.target) && e.target !== anchor) closeTimePicker();
  };
  const place = () => {
    if (sheet || !anchor) return;
    const r = anchor.getBoundingClientRect();
    const w = panel.offsetWidth;
    const left = Math.max(8, Math.min(window.innerWidth - w - 8, r.left + r.width / 2 - w / 2));
    const below = r.bottom + 8;
    const top = below + panel.offsetHeight > window.innerHeight - 8 ? Math.max(8, r.top - panel.offsetHeight - 8) : below;
    panel.style.left = `${Math.round(left)}px`;
    panel.style.top = `${Math.round(top)}px`;
  };
  const onResize = () => place();
  document.addEventListener('keydown', onKey, true);
  document.addEventListener('pointerdown', onDocDown, true);
  window.addEventListener('resize', onResize);
  // Typing into the field: a complete time moves this side's wheels.
  const onTyped = () => {
    const raw = typing.value.trim();
    if (!/^\d{4}$/.test(raw) && !/^\d{1,2}\s*[:.\sh]\s*\d{2}$/i.test(raw)) return;
    const norm = normalizeTypedTime(raw);
    if (!norm) return;
    values[side] = clampV(parseHHMM(norm));
    loadSide(false);
  };
  typing?.addEventListener('input', onTyped);
  openState = { root, anchor, onKey, onDocDown, onResize, typing, onTyped };

  place();
  loadSide(false);
  markSelected(hWheel); markSelected(mWheel);
  if (!typing) hWheel.focus({ preventScroll: true });
}

/** True while the picker is open for this field. */
export function timePickerOpenFor(el) {
  return !!openState && openState.anchor === el;
}
