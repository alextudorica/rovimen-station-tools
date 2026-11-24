import { describe, it, expect, vi, afterEach } from 'vitest';

import { normalizeTypedTime, wireTimeField, openTimePicker, closeTimePicker } from '../../dashboard/static/dashboard-time-picker.js';

describe('normalizeTypedTime (what a typed time field accepts)', () => {
  it.each([
    ['2333', '23:33'],
    ['933', '09:33'],
    ['0005', '00:05'],
    ['23', '23:00'],
    ['9', '09:00'],
    ['23:33', '23:33'],
    ['23.33', '23:33'],
    ['23 33', '23:33'],
    ['7:5', '07:05'],
    [' 1845 ', '18:45'],
  ])('%s -> %s', (typed, want) => {
    expect(normalizeTypedTime(typed)).toBe(want);
  });

  it.each(['2400', '2360', '24', '12345', 'ab', '', '12:345', '-1'])('rejects %j', (typed) => {
    expect(normalizeTypedTime(typed)).toBeNull();
  });
});

function field(value = '18:54') {
  const el = document.createElement('input');
  el.type = 'text';
  el.value = value;
  document.body.appendChild(el);
  return el;
}

function setPhone(phone) {
  window.matchMedia = vi.fn().mockReturnValue({
    matches: phone, addEventListener: () => {}, removeEventListener: () => {},
  });
}

describe('wireTimeField on desktop: click and type', () => {
  afterEach(() => { document.body.innerHTML = ''; delete window.matchMedia; });

  it('is editable, and a typed "2333" reaches the page as 23:33', () => {
    setPhone(false);
    const el = field();
    const seen = [];
    wireTimeField(el, { which: 'start', openPicker: () => {} });
    el.addEventListener('change', () => seen.push(el.value));
    expect(el.readOnly).toBe(false);
    el.dispatchEvent(new Event('focus'));
    el.value = '2333';
    el.dispatchEvent(new Event('change'));
    expect(seen).toEqual(['23:33']);
    expect(el.value).toBe('23:33');
  });

  it('a click brings the wheels up in typing mode (the field keeps the caret)', () => {
    setPhone(false);
    const el = field();
    const open = vi.fn();
    wireTimeField(el, { which: 'start', openPicker: open });
    el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    expect(open).toHaveBeenCalledWith('start', { typing: el });
  });

  it('a non-time puts the old value back and the page never sees it', () => {
    setPhone(false);
    const el = field();
    const seen = [];
    wireTimeField(el, { which: 'start', openPicker: () => {} });
    el.addEventListener('change', () => seen.push(el.value));
    el.dispatchEvent(new Event('focus'));
    el.value = '2399';
    el.dispatchEvent(new Event('change'));
    expect(seen).toEqual([]);
    expect(el.value).toBe('18:54');
    expect(el.classList.contains('invalid')).toBe(true);
  });

  it('Escape restores the value; ArrowDown opens the wheels for this end', () => {
    setPhone(false);
    const el = field();
    const open = vi.fn();
    wireTimeField(el, { which: 'end', openPicker: open });
    el.dispatchEvent(new Event('focus'));
    el.value = '12';
    el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(el.value).toBe('18:54');
    el.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown' }));
    expect(open).toHaveBeenCalledWith('end');
  });
});

describe('wireTimeField on phones: a tap opens the wheel sheet', () => {
  afterEach(() => { document.body.innerHTML = ''; delete window.matchMedia; });

  it('is read-only and opens the picker on click', () => {
    setPhone(true);
    const el = field();
    const open = vi.fn();
    wireTimeField(el, { which: 'start', openPicker: open });
    expect(el.readOnly).toBe(true);
    el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    expect(open).toHaveBeenCalledWith('start');
  });
});


describe('openTimePicker in typing mode', () => {
  // jsdom has no scrolling; the wheels only need scrollTo to exist.
  Element.prototype.scrollTo ??= function scrollTo(o) { this.scrollTop = o?.top ?? 0; };
  afterEach(() => { closeTimePicker(); document.body.innerHTML = ''; delete window.matchMedia; });

  it('leaves focus in the field and moves the wheels to a complete typed time', () => {
    setPhone(false);
    const el = field('18:54');
    el.focus();
    openTimePicker({ anchor: el, start: '18:54', end: '18:59', focus: 'start', typing: el, onSet: () => {} });
    expect(document.activeElement).toBe(el);
    el.value = '233';                                   // not complete yet: wheels stay
    el.dispatchEvent(new Event('input'));
    expect(document.querySelector('.tp-side[data-side="start"] b').textContent).toBe('18:54');
    el.value = '2333';
    el.dispatchEvent(new Event('input'));
    expect(document.querySelector('.tp-side[data-side="start"] b').textContent).toBe('23:33');
  });

  it('Set applies what was typed', () => {
    setPhone(false);
    const el = field('18:54');
    const onSet = vi.fn();
    openTimePicker({ anchor: el, start: '18:54', end: '18:59', focus: 'start', typing: el, onSet });
    el.value = '23:33';
    el.dispatchEvent(new Event('input'));
    document.querySelector('.tp-set').click();
    expect(onSet).toHaveBeenCalledWith('23:33', '18:59');
  });
});

describe('openTimePicker limited to the capture window', () => {
  Element.prototype.scrollTo ??= function scrollTo(o) { this.scrollTop = o?.top ?? 0; };
  afterEach(() => { closeTimePicker(); document.body.innerHTML = ''; delete window.matchMedia; });
  // footage 15:39 → 05:11 UTC, in the sliders' noon-anchored minutes
  const limits = { lo: 15 * 60 + 39 - 720, hi: 5 * 60 + 11 + 720 };
  const hours = () => [...document.querySelectorAll('.tp-wheel-h .tp-item')].map(i => +i.dataset.v);
  const label = (side) => document.querySelector(`.tp-side[data-side="${side}"] b`).textContent;

  it('offers only the hours of the window, in night order', () => {
    setPhone(false);
    openTimePicker({ anchor: field(), start: '18:00', end: '02:00', limits, onSet: () => {} });
    expect(hours()).toEqual([15, 16, 17, 18, 19, 20, 21, 22, 23, 0, 1, 2, 3, 4, 5]);
  });

  it('pulls times outside it to its ends, and Set returns them', () => {
    setPhone(false);
    const onSet = vi.fn();
    openTimePicker({ anchor: field(), start: '12:30', end: '09:00', limits, onSet });
    expect(label('start')).toBe('15:39');
    expect(label('end')).toBe('05:11');
    document.querySelector('.tp-set').click();
    expect(onSet).toHaveBeenCalledWith('15:39', '05:11');
  });

  it('ignores a typed hour outside the window', () => {
    setPhone(false);
    openTimePicker({ anchor: field(), start: '18:00', end: '02:00', limits, onSet: () => {} });
    const key = (k) => document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true }));
    key('7');                // 07 is daytime and cannot start a two-digit hour
    expect(label('start')).toBe('18:00');
    key('2'); key('1');      // 21
    key('1'); key('5');      // :15
    expect(label('start')).toBe('21:15');
  });

  it('pulls a typed time before the start up to it', () => {
    setPhone(false);
    openTimePicker({ anchor: field(), start: '18:00', end: '02:00', limits, onSet: () => {} });
    const key = (k) => document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true }));
    key('1'); key('5'); key('2'); key('0');   // 15:20, before the 15:39 start
    expect(label('start')).toBe('15:39');
  });
});
