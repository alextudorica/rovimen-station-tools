import { describe, it, expect, beforeAll } from 'vitest';

// dashboard-calibration.js kicks off an /api/auth/status fetch at module
// load and registers a DOMContentLoaded handler; neither needs the page to
// exist for the pure helpers under test here.
let cal;
let editor;
beforeAll(async () => {
  global.fetch = async () => ({ json: async () => ({}) });
  cal = await import('../../dashboard/static/dashboard-calibration.js');
  editor = await import('../../dashboard/static/dashboard-mask-editor.js');
});

describe('parseNightDate', () => {
  it('parses YYYYMMDD as a UTC midnight', () => {
    expect(cal.parseNightDate('20260920')).toBe(Date.UTC(2026, 8, 20));
  });

  it('rejects anything that is not eight digits', () => {
    for (const bad of ['2026-09-20', '', null, undefined, '2026092', 'not a date']) {
      expect(cal.parseNightDate(bad)).toBeNull();
    }
  });
});

describe('nightsBehind', () => {
  it('is zero when the archived night is the expected one', () => {
    expect(cal.nightsBehind('20260920', '20260920')).toBe(0);
  });

  it('counts whole nights across a month boundary', () => {
    expect(cal.nightsBehind('20260822', '20260920')).toBe(29);
  });

  // The fleet runs on UTC but browsers do not; a local-time Date would drop
  // or add a night across a DST switch, which is exactly the window when
  // operators are most likely to be reading this page.
  it('is unaffected by a DST boundary between the two dates', () => {
    expect(cal.nightsBehind('20261024', '20261027')).toBe(3);
  });

  it('returns null when either date is missing', () => {
    expect(cal.nightsBehind(null, '20260920')).toBeNull();
    expect(cal.nightsBehind('20260920', null)).toBeNull();
  });
});

describe('nightStatus', () => {
  const ok = (date, expected) => ({ status: 'ok', date, expected_date: expected });

  it('reports the night plainly when it is the expected one', () => {
    const s = cal.nightStatus([ok('20260920', '20260920'), ok('20260920', '20260920')]);
    expect(s.text).toBe('Night 2026-09-20');
    expect(s.cls).toBe('');
  });

  it('takes the newest night when the plots disagree', () => {
    const s = cal.nightStatus([ok('20260918', '20260920'), ok('20260920', '20260920')]);
    expect(s.text).toBe('Night 2026-09-20');
  });

  it('says how far behind a stale card is, and warns at one night', () => {
    const s = cal.nightStatus([ok('20260919', '20260920')]);
    expect(s.text).toBe('Night 2026-09-19 — 1 night behind');
    expect(s.cls).toBe('cal-stale-warn');
  });

  it('escalates to bad from three nights behind', () => {
    const s = cal.nightStatus([ok('20260917', '20260920')]);
    expect(s.text).toBe('Night 2026-09-17 — 3 nights behind');
    expect(s.cls).toBe('cal-stale-bad');
  });

  it('marks a camera with nothing archived at all', () => {
    const s = cal.nightStatus([
      { status: 'no_data', date: null, expected_date: '20260920' },
      { status: 'no_data', date: null, expected_date: '20260920' },
    ]);
    expect(s.text).toBe('Night: no plots archived');
    expect(s.cls).toBe('cal-stale-bad');
  });

  it('survives a plot fetch that failed outright', () => {
    const s = cal.nightStatus([null, null, ok('20260920', '20260920')]);
    expect(s.text).toBe('Night 2026-09-20');
  });

  it('never claims a card is ahead of the expected night', () => {
    const s = cal.nightStatus([ok('20260921', '20260920')]);
    expect(s.cls).toBe('');
    expect(s.text).toBe('Night 2026-09-21');
  });
});

describe('maskedPercentFromCanvas', () => {
  // A fake 2D context standing in for canvas.getContext('2d') -- returns a
  // flat RGBA buffer, red channel only meaningful (mask.bmp is greyscale,
  // R=G=B, matching what the station itself writes on save).
  const fakeCtx = (reds) => ({
    getImageData: () => ({
      data: Uint8ClampedArray.from(reds.flatMap(v => [v, v, v, 255])),
    }),
  });

  it('reads 0% for an all-active (white) mask', () => {
    expect(cal.maskedPercentFromCanvas(fakeCtx([255, 255, 255, 255]), 2, 2)).toBe(0);
  });

  it('reads 100% for an all-excluded (black) mask', () => {
    expect(cal.maskedPercentFromCanvas(fakeCtx([0, 0, 0, 0]), 2, 2)).toBe(100);
  });

  it('counts a quarter-masked frame as 25%', () => {
    expect(cal.maskedPercentFromCanvas(fakeCtx([0, 255, 255, 255]), 2, 2)).toBe(25);
  });

  it('treats the same 128 threshold the station binarises on save with', () => {
    // station_api.py api_mask_write: `0 if v < 128 else 255` -- 128 itself
    // counts as active, matching a strict less-than on both sides.
    expect(cal.maskedPercentFromCanvas(fakeCtx([127, 128]), 2, 1)).toBe(50);
  });

  it('returns null rather than throwing on a zero-sized image', () => {
    expect(cal.maskedPercentFromCanvas(fakeCtx([]), 0, 0)).toBeNull();
  });

  it('returns null instead of throwing when the canvas read fails', () => {
    const throwingCtx = { getImageData: () => { throw new Error('tainted canvas'); } };
    expect(cal.maskedPercentFromCanvas(throwingCtx, 2, 2)).toBeNull();
  });
});

describe('mask editor brush geometry', () => {
  const grid = (w, h) => ({ bits: new Uint8Array(w * h), w, h });

  it('paints a filled disc and nothing outside it', () => {
    const { bits, w, h } = grid(21, 21);
    editor.stampCircle(bits, w, h, 10, 10, 5, 1, editor.emptyDirty());
    expect(bits[10 * w + 10]).toBe(1);           // centre
    expect(bits[10 * w + 15]).toBe(1);           // on the radius
    expect(bits[10 * w + 16]).toBe(0);           // just outside
    expect(bits[0]).toBe(0);                     // corner untouched
  });

  it('clips at the frame edge instead of wrapping to the next row', () => {
    const { bits, w, h } = grid(20, 20);
    editor.stampCircle(bits, w, h, 0, 5, 4, 1, editor.emptyDirty());
    expect(bits[5 * w + 0]).toBe(1);
    expect(bits[5 * w + (w - 1)]).toBe(0);       // would be the wrap victim
    expect(bits[4 * w + (w - 1)]).toBe(0);
  });

  it('erases with the same footprint it paints', () => {
    const { bits, w, h } = grid(21, 21);
    bits.fill(1);
    editor.stampCircle(bits, w, h, 10, 10, 5, 0, editor.emptyDirty());
    expect(bits[10 * w + 10]).toBe(0);
    expect(bits[0]).toBe(1);
  });

  it('grows the dirty rect to cover every stamp in a stroke', () => {
    const { bits, w, h } = grid(60, 60);
    const dirty = editor.emptyDirty();
    editor.stampCircle(bits, w, h, 10, 10, 3, 1, dirty);
    editor.stampCircle(bits, w, h, 40, 30, 3, 1, dirty);
    expect(dirty.x0).toBe(7);
    expect(dirty.y0).toBe(7);
    expect(dirty.x1).toBe(43);
    expect(dirty.y1).toBe(33);
  });

  it('leaves the dirty rect empty when the brush misses the frame entirely', () => {
    const { bits, w, h } = grid(20, 20);
    const dirty = editor.emptyDirty();
    editor.stampCircle(bits, w, h, -50, -50, 3, 1, dirty);
    expect(dirty.x1 < dirty.x0 || dirty.y1 < dirty.y0).toBe(true);
    expect(bits.some(v => v === 1)).toBe(false);
  });
});
