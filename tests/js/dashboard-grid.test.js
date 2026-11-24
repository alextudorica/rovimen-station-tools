import { describe, it, expect, afterEach } from 'vitest';
import { state } from '../../dashboard/static/dashboard-common.js';

import {
  gridCamUrl,
  gridStationUrl,
  _gridAgeLabel,
  _gridFilterColumns,
  _gridVisibleColumns,
  _gridStationLabel,
  _footageCellHtml,
  _footageStripHtml,
  _gridIsOther,
  _gridDefaultFromMin,
  _gridClampToWindow,
  _gridResolveThumb,
  _gridClipKey,
  _gridClipToVideo,
  _gridSelectionVideos,
  _gridCellHtml,
  GRID_SELECT_MAX,
  _gridThumbImgFailed,
  _gridParseCams,
  gridCamsParam,
} from '../../dashboard/static/dashboard-grid.js';

describe('_gridDefaultFromMin (footage default = now-5min UTC, not 22:00)', () => {
  it('returns the current UTC minute-of-day minus 5, never the old 22:00 constant', () => {
    // 14:30 UTC -> 14:25 = 865 min. Regression guard against the hardcoded
    // 22*60 = 1320 that used to sit here.
    const at1430 = new Date('2026-08-01T14:30:00Z');
    expect(_gridDefaultFromMin(at1430)).toBe(14 * 60 + 25);
    expect(_gridDefaultFromMin(at1430)).not.toBe(22 * 60);
  });
  it('ignores local timezone — computes purely in UTC', () => {
    // A Date built from a +03:00 wall time is 19:00 UTC; default = 18:55.
    const tzAware = new Date('2026-08-01T22:00:00+03:00');
    expect(_gridDefaultFromMin(tzAware)).toBe(18 * 60 + 55);
  });
  it('wraps across midnight instead of going negative', () => {
    expect(_gridDefaultFromMin(new Date('2026-08-01T00:02:00Z'))).toBe(23 * 60 + 57);
    expect(_gridDefaultFromMin(new Date('2026-08-01T00:00:00Z'))).toBe(23 * 60 + 55);
    expect(_gridDefaultFromMin(new Date('2026-08-01T00:05:00Z'))).toBe(0);
  });
  it('defaults to real now when called with no argument (dynamic, not a constant)', () => {
    const now = new Date();
    const expected = ((now.getUTCHours() * 60 + now.getUTCMinutes() - 5) % 1440 + 1440) % 1440;
    // Allow a 1-minute slack for the clock ticking between the two reads.
    const got = _gridDefaultFromMin();
    const diff = Math.min((got - expected + 1440) % 1440, (expected - got + 1440) % 1440);
    expect(diff).toBeLessThanOrEqual(1);
  });
});

describe('_gridResolveThumb (grid station click resolution)', () => {
  it('resolves a playable thumb for ANY station, foreign codes included', () => {
    // Berlin ("other station") camera must resolve exactly like a RO one.
    const de = _gridResolveThumb({
      host: 'gmnde00', cam: 'DE001B', date: '20260801',
      fn: 'DE001B_20260801_221530_color.mkv', offset: '3.5', mtime: '2026-08-01T22:15:33',
      stack: 'DE001B_20260801_221530_stack.webp', label: 'Berlin',
    });
    expect(de).toEqual({
      host: 'gmnde00', cam: 'DE001B', date: '20260801',
      fn: 'DE001B_20260801_221530_color.mkv', offset: 3.5, mtime: '2026-08-01T22:15:33',
      stack: 'DE001B_20260801_221530_stack.webp', label: 'Berlin', reencoded: false,
      locked: false, lockType: null, hidden: false,
    });
    const ro = _gridResolveThumb({
      host: 'gmnro02', cam: 'RO000M', date: '20260801',
      fn: 'RO000M_20260801_221530_color.mkv', offset: '0', mtime: '',
    });
    expect(ro.host).toBe('gmnro02');
    expect(ro.fn).toBe('RO000M_20260801_221530_color.mkv');
    expect(ro.offset).toBe(0);
  });
  it('coerces a missing/garbage offset to 0', () => {
    const r = _gridResolveThumb({ host: 'h', cam: 'RO000M', date: '20260801', fn: 'x.mkv' });
    expect(r.offset).toBe(0);
    const g = _gridResolveThumb({ host: 'h', cam: 'RO000M', date: '20260801', fn: 'x.mkv', offset: 'NaN' });
    expect(g.offset).toBe(0);
  });
  it('no-ops (null) on a pending cell with no clip filename', () => {
    expect(_gridResolveThumb({ host: 'gmnro02', cam: 'RO000M', date: '20260801', fn: '' })).toBeNull();
    expect(_gridResolveThumb({ host: 'gmnro02', cam: 'RO000M', date: '20260801' })).toBeNull();
  });
  it('no-ops (null) when the station key is missing, avoiding a broken video URL', () => {
    expect(_gridResolveThumb({ host: '', cam: 'RO000M', date: '20260801', fn: 'x.mkv' })).toBeNull();
    expect(_gridResolveThumb({ cam: 'RO000M', date: '20260801', fn: 'x.mkv' })).toBeNull();
  });
  it('tolerates null/undefined dataset', () => {
    expect(_gridResolveThumb(null)).toBeNull();
    expect(_gridResolveThumb(undefined)).toBeNull();
  });
});

describe('_gridIsOther', () => {
  it('flags cameras outside Romania/Moldova (e.g. Berlin)', () => {
    expect(_gridIsOther('DE001B')).toBe(true);
    expect(_gridIsOther('DE0018')).toBe(true);
    expect(_gridIsOther('de001c')).toBe(true);
  });
  it('does not flag Romania/Moldova (RO/MD/OR) codes', () => {
    expect(_gridIsOther('RO000M')).toBe(false);
    expect(_gridIsOther('MD0001')).toBe(false);
    expect(_gridIsOther('OR0001')).toBe(false);
    expect(_gridIsOther('')).toBe(false);
    expect(_gridIsOther(null)).toBe(false);
  });
});

describe('_gridAgeLabel', () => {
  const now = Date.parse('2026-06-16T12:00:00Z');
  it('returns "just now" under a minute', () => {
    expect(_gridAgeLabel('2026-06-16T11:59:30', now)).toBe('just now');
  });
  it('returns minutes', () => {
    expect(_gridAgeLabel('2026-06-16T11:30:00', now)).toBe('30m ago');
  });
  it('returns hours', () => {
    expect(_gridAgeLabel('2026-06-16T10:00:00', now)).toBe('2h ago');
  });
  it('returns days', () => {
    expect(_gridAgeLabel('2026-06-14T12:00:00', now)).toBe('2d ago');
  });
  it('handles a trailing Z without double-appending', () => {
    expect(_gridAgeLabel('2026-06-16T10:00:00Z', now)).toBe('2h ago');
  });
  it('clamps a future timestamp to "just now"', () => {
    expect(_gridAgeLabel('2026-06-16T13:00:00', now)).toBe('just now');
  });
  it('returns empty string for falsy / unparseable input', () => {
    expect(_gridAgeLabel('', now)).toBe('');
    expect(_gridAgeLabel(null, now)).toBe('');
    expect(_gridAgeLabel('not-a-date', now)).toBe('');
  });
});

describe('_gridFilterColumns', () => {
  const columns = [
    { cam: 'RO000A', host_key: 'gmnro01', station_label: 'Ghirdoveni' },
    { cam: 'RO000B', host_key: 'gmnro01', station_label: 'Ghirdoveni' },
    { cam: 'RO000M', host_key: 'gmnro02', station_label: 'Vaslui' },
  ];
  it('keeps only columns with at least one cell, preserving order', () => {
    const rows = [
      { cells: { RO000A: [{}], RO000M: [{}] } },
      { cells: { RO000A: [{}] } },
    ];
    const kept = _gridFilterColumns(columns, rows);
    expect(kept.map(c => c.cam)).toEqual(['RO000A', 'RO000M']);
  });
  it('returns empty array when no rows', () => {
    expect(_gridFilterColumns(columns, [])).toEqual([]);
  });
  it('tolerates missing cells objects', () => {
    expect(_gridFilterColumns(columns, [{}])).toEqual([]);
  });
  it('tolerates null inputs', () => {
    expect(_gridFilterColumns(null, null)).toEqual([]);
  });
  it('handles footage cells (object-valued), not just detection arrays', () => {
    const rows = [{ cells: { RO000B: { count: 3, rep: {}, chunks: [] } } }];
    expect(_gridFilterColumns(columns, rows).map(c => c.cam)).toEqual(['RO000B']);
  });
});

describe('_gridVisibleColumns', () => {
  const columns = [
    { cam: 'DE001B', host_key: 'gmnde00', station_label: 'Johannismühle' },
    { cam: 'MD0001', host_key: 'gmnmd01', station_label: 'Cahul' },
    { cam: 'RO000A', host_key: 'gmnro01', station_label: 'Ghirdoveni' },
    { cam: 'RO000M', host_key: 'gmnro02', station_label: 'Vaslui' },
  ];
  const rows = [{ cells: { DE001B: { chunks: [{}] }, RO000M: { chunks: [{}] } } }];
  it('drops empty cameras by default (detections grid)', () => {
    const cols = _gridVisibleColumns(columns, rows, { showOther: true });
    expect(cols.map(c => c.cam)).toEqual(['DE001B', 'RO000M']);
  });
  it('keeps empty cameras in station order with keepEmpty (footage grid)', () => {
    const cols = _gridVisibleColumns(columns, rows, { keepEmpty: true, showOther: true });
    expect(cols.map(c => c.cam)).toEqual(['DE001B', 'MD0001', 'RO000A', 'RO000M']);
  });
  it('still hides "other" cameras unless the toggle is on', () => {
    const cols = _gridVisibleColumns(columns, rows, { keepEmpty: true, showOther: false });
    expect(cols.map(c => c.cam)).toEqual(['MD0001', 'RO000A', 'RO000M']);
  });
  it('tolerates null inputs', () => {
    expect(_gridVisibleColumns(null, null, { keepEmpty: true })).toEqual([]);
  });
  it('shows exactly the map-picked cameras, a foreign one included', () => {
    const onlyCams = new Set(['DE001B', 'RO000A']);
    const cols = _gridVisibleColumns(columns, rows, { keepEmpty: true, showOther: false, onlyCams });
    expect(cols.map(c => c.cam)).toEqual(['DE001B', 'RO000A']);
  });
  it('still drops empty picked cameras in the detections grid', () => {
    const onlyCams = new Set(['DE001B', 'RO000A']);
    expect(_gridVisibleColumns(columns, rows, { onlyCams }).map(c => c.cam)).toEqual(['DE001B']);
  });
  it('treats an empty pick as no filter', () => {
    const cols = _gridVisibleColumns(columns, rows, { keepEmpty: true, showOther: true, onlyCams: new Set() });
    expect(cols).toHaveLength(4);
  });
});

describe('_gridParseCams (the ?cams= list from a map point)', () => {
  it('upper-cases, de-duplicates and keeps order', () => {
    expect(_gridParseCams('ro000e,RO000G, RO000E')).toEqual(['RO000E', 'RO000G']);
  });
  it('drops anything that is not a camera code', () => {
    expect(_gridParseCams('RO000E,<script>,,RO000G;x')).toEqual(['RO000E']);
  });
  it('is null for an empty or missing value', () => {
    expect(_gridParseCams('')).toBeNull();
    expect(_gridParseCams(null)).toBeNull();
    expect(_gridParseCams(',,')).toBeNull();
  });
  it('without ?cams= the grid has no camera filter', () => {
    expect(gridCamsParam()).toBeNull();
  });
});

describe('_gridStationLabel', () => {
  it('joins the station ID and the place', () => {
    expect(_gridStationLabel({ host_key: 'gmnro10', station_label: 'Roit' })).toBe('gmnro10 · Roit');
  });
  it('falls back to whichever part is present', () => {
    expect(_gridStationLabel({ host_key: 'gmnro10' })).toBe('gmnro10');
    expect(_gridStationLabel({ station_label: 'Roit' })).toBe('Roit');
    expect(_gridStationLabel(null)).toBe('');
  });
  it('does not repeat a label equal to the ID', () => {
    expect(_gridStationLabel({ host_key: 'gmnro23', station_label: 'gmnro23' })).toBe('gmnro23');
  });
});

describe('_footageCellHtml', () => {
  const mkChunk = (over = {}) => ({
    host_key: 'gmnro02', filename: 'RO000M_20260622_220500_color.mkv',
    time: '20:05:00', stack: 'RO000M_20260622_220500_stack.webp',
    locked: false, meteor_time: null, detection_offset_s: 0, ...over,
  });
  const cell = {
    host_key: 'gmnro02', count: 3, has_detection: false,
    chunks: [mkChunk(), mkChunk({ filename: 'b.mkv', time: '20:05:20' }), mkChunk({ filename: 'c.mkv', time: '20:05:40' })],
  };
  it('renders an empty cell when there is no footage', () => {
    expect(_footageCellHtml('20260622', 'RO000M', null))
      .toBe('<td class="grid-cell grid-cell-empty"></td>');
    expect(_footageCellHtml('20260622', 'RO000M', { count: 0, chunks: [] }))
      .toBe('<td class="grid-cell grid-cell-empty"></td>');
  });
  it('renders EVERY clip in the cell (one playable thumb each)', () => {
    const html = _footageCellHtml('20260622', 'RO000M', cell);
    expect(html).toContain('grid-cell-footage');
    expect(html).toContain('/stack/gmnro02/RO000M/20260622/RO000M_20260622_220500_stack.webp');
    expect((html.match(/data-fn="/g) || []).length).toBe(3);   // all 3 clips, no rep/expand
    expect(html).not.toContain('grid-expand');                 // no expand button anymore
  });
  it('marks cells that contain a detection', () => {
    const det = { ...cell, has_detection: true };
    expect(_footageCellHtml('20260622', 'RO000M', det)).toContain('grid-cell-detection');
  });
  it('escapes interpolated filename values (no raw markup)', () => {
    const evil = { ...cell, chunks: [mkChunk({ filename: '"><img src=x onerror=alert(1)>' })] };
    expect(_footageCellHtml('20260622', 'RO000M', evil)).not.toContain('<img src=x onerror=');
  });
});

describe('_footageStripHtml', () => {
  const chunks = [
    { host_key: 'gmnro02', filename: 'a_color.mkv', time: '20:05:00',
      stack: 'a_stack.webp', locked: false, detection_offset_s: 0, meteor_time: null },
    { host_key: 'gmnro02', filename: 'b_color.mkv', time: '20:05:20',
      stack: null, locked: true, detection_offset_s: 7.2, meteor_time: '2026-06-22T20:05:25' },
  ];
  it('renders one thumb per chunk with its filename', () => {
    const html = _footageStripHtml('20260622', 'RO000M', chunks);
    expect(html).toContain('data-fn="a_color.mkv"');
    expect(html).toContain('data-fn="b_color.mkv"');
    // One data-fn attribute per chunk thumb (avoids matching grid-thumb-time etc).
    expect((html.match(/data-fn="/g) || []).length).toBe(2);
  });
  it('flags the locked chunk and falls back to "no img" without a stack', () => {
    const html = _footageStripHtml('20260622', 'RO000M', chunks);
    expect(html).toContain('grid-thumb detection');
    expect(html).toContain('no img');
  });
  it('returns empty string for no chunks', () => {
    expect(_footageStripHtml('20260622', 'RO000M', [])).toBe('');
    expect(_footageStripHtml('20260622', 'RO000M', null)).toBe('');
  });
  it('threads the station label + stack onto each thumb (for multi-select)', () => {
    const html = _footageStripHtml('20260622', 'RO000M', chunks, 'Vaslui');
    expect((html.match(/data-label="Vaslui"/g) || []).length).toBe(2);
    expect(html).toContain('data-stack="a_stack.webp"');
    expect(html).toContain('data-stack=""'); // second chunk has no stack
  });
  it('marks a re-encoded chunk so its modal shows Processed, not Process clip', () => {
    const html = _footageStripHtml('20260622', 'RO000M', [{ ...chunks[1], reencoded: true }, chunks[0]]);
    expect(html).toContain('data-reencoded="1"');
    expect(html).toContain('data-reencoded=""');
    const el = document.createElement('div');
    el.innerHTML = html;
    const [done, raw] = el.querySelectorAll('.grid-thumb');
    expect(_gridResolveThumb(done.dataset).reencoded).toBe(true);
    expect(_gridResolveThumb(raw.dataset).reencoded).toBe(false);
  });
});

describe('_gridResolveThumb — stack/label additions (multi-select)', () => {
  it('carries stack + label through, defaulting both to empty string', () => {
    const withMeta = _gridResolveThumb({
      host: 'gmnro02', cam: 'RO000M', date: '20260801', fn: 'x.mkv',
      stack: 'x_stack.webp', label: 'Vaslui',
    });
    expect(withMeta.stack).toBe('x_stack.webp');
    expect(withMeta.label).toBe('Vaslui');
    const bare = _gridResolveThumb({ host: 'h', cam: 'RO000M', date: '20260801', fn: 'x.mkv' });
    expect(bare.stack).toBe('');
    expect(bare.label).toBe('');
  });
});

describe('_gridResolveThumb — real lock state (regression: Lock button always showed "Locked")', () => {
  it('carries an unlocked footage chunk through as unlocked', () => {
    const unlocked = _gridResolveThumb({
      host: 'gmnro02', cam: 'RO000M', date: '20260801', fn: 'x.mkv', locked: '0',
    });
    expect(unlocked.locked).toBe(false);
    expect(unlocked.lockType).toBeNull();
  });
  it('carries a locked chunk through with its lock type', () => {
    const locked = _gridResolveThumb({
      host: 'gmnro02', cam: 'RO000M', date: '20260801', fn: 'x.mkv',
      locked: '1', lockType: 'manual',
    });
    expect(locked.locked).toBe(true);
    expect(locked.lockType).toBe('manual');
  });
});

describe('_gridClipKey (selection identity)', () => {
  it('is stable across renders and collapses the same clip', () => {
    const a = { host: 'gmnro02', cam: 'RO000M', date: '20260801', fn: 'x.mkv', offset: 3 };
    const b = { host: 'gmnro02', cam: 'RO000M', date: '20260801', fn: 'x.mkv', offset: 9, stack: 's' };
    expect(_gridClipKey(a)).toBe('gmnro02|RO000M|20260801|x.mkv');
    expect(_gridClipKey(a)).toBe(_gridClipKey(b)); // offset/stack don't change identity
  });
  it('distinguishes clips that differ in any of host/cam/date/filename', () => {
    const base = { host: 'h', cam: 'C', date: 'd', fn: 'f' };
    expect(_gridClipKey({ ...base, host: 'h2' })).not.toBe(_gridClipKey(base));
    expect(_gridClipKey({ ...base, cam: 'C2' })).not.toBe(_gridClipKey(base));
    expect(_gridClipKey({ ...base, date: 'd2' })).not.toBe(_gridClipKey(base));
    expect(_gridClipKey({ ...base, fn: 'f2' })).not.toBe(_gridClipKey(base));
  });
  it('tolerates null / partial clips', () => {
    expect(_gridClipKey(null)).toBe('');
    expect(_gridClipKey({})).toBe('|||');
  });
});

describe('_gridClipToVideo (selection → MultiDetModal shape)', () => {
  it('builds the exact video shape openEventModal uses', () => {
    const v = _gridClipToVideo({
      host: 'gmnro02', cam: 'RO000M', date: '20260801',
      fn: 'RO000M_20260801_221530_color.mkv', offset: '3.5',
      stack: 'RO000M_20260801_221530_stack.webp', label: 'Vaslui',
    });
    expect(v).toEqual({
      cam: 'RO000M',
      station: 'gmnro02',
      date: '20260801',
      url: '/api/cached-video/gmnro02/RO000M/20260801/RO000M_20260801_221530_color.mkv?format=mp4',
      offset: 3.5,
      label: 'Vaslui',
      downloadUrl: '/shortclip/gmnro02/RO000M/20260801/RO000M_20260801_221530_color.mkv',
      stackUrl: '/stack/gmnro02/RO000M/20260801/RO000M_20260801_221530_stack.webp',
      filename: 'RO000M_20260801_221530_color.mkv',
    });
  });
  it('falls back label -> cam and stackUrl -> null when absent', () => {
    const v = _gridClipToVideo({ host: 'h', cam: 'RO000M', date: '20260801', fn: 'x.mkv' });
    expect(v.label).toBe('RO000M');
    expect(v.stackUrl).toBeNull();
    expect(v.offset).toBe(0);
  });
  it('url-encodes the filename', () => {
    const v = _gridClipToVideo({ host: 'h', cam: 'C', date: 'd', fn: 'a b.mkv' });
    expect(v.url).toContain('a%20b.mkv');
    expect(v.downloadUrl).toContain('a%20b.mkv');
  });
  it('gives a detection clip its Hide (by camera and meteor time), and footage none', () => {
    const det = _gridClipToVideo({ host: 'h', cam: 'RO000M', date: '20260801', fn: 'x.mkv',
      lockType: 'detection', mtime: '2026-08-01T22:15:33.4', hidden: true });
    expect(det.hide).toEqual({ target: { cam: 'RO000M', time_utc: '2026-08-01T22:15:33' }, hidden: true });
    const footage = _gridClipToVideo({ host: 'h', cam: 'RO000M', date: '20260801', fn: 'x.mkv',
      lockType: null, mtime: '' });
    expect('hide' in footage).toBe(false);
  });
  it('returns null for a clip that cannot be played (no host / no filename)', () => {
    expect(_gridClipToVideo(null)).toBeNull();
    expect(_gridClipToVideo({ host: '', cam: 'C', date: 'd', fn: 'x.mkv' })).toBeNull();
    expect(_gridClipToVideo({ host: 'h', cam: 'C', date: 'd', fn: '' })).toBeNull();
  });
});

describe('_gridSelectionVideos (ordered payload builder)', () => {
  const mk = (fn, over = {}) => ({ host: 'h', cam: 'C', date: 'd', fn, ...over });
  it('preserves selection order and skips unplayable clips', () => {
    const clips = [mk('a.mkv'), { host: '', cam: 'C', date: 'd', fn: 'bad.mkv' }, mk('b.mkv')];
    const vids = _gridSelectionVideos(clips);
    expect(vids.map(v => v.filename)).toEqual(['a.mkv', 'b.mkv']);
  });
  it('accepts a Map .values() iterator (the real selection store)', () => {
    const m = new Map([['k1', mk('a.mkv')], ['k2', mk('b.mkv')]]);
    expect(_gridSelectionVideos(m.values()).length).toBe(2);
  });
  it('tolerates null / empty input', () => {
    expect(_gridSelectionVideos(null)).toEqual([]);
    expect(_gridSelectionVideos([])).toEqual([]);
  });
  it('exports a sane selection cap', () => {
    expect(GRID_SELECT_MAX).toBeGreaterThan(1);
    expect(Number.isInteger(GRID_SELECT_MAX)).toBe(true);
  });
});


describe('_gridCellHtml pending labels', () => {
  const cell = (extra) => ({
    host_key: 'gmnro17', cam: 'RO0001', meteor_time: '2026-09-14T20:11:02',
    filename: null, stack: null, ...extra,
  });

  it('says "not uploaded yet" while a clip is genuinely in transit', () => {
    const html = _gridCellHtml('20260914', 'RO0001', [cell({ pending: true })]);
    expect(html).toContain('>not uploaded yet<');
    expect(html).not.toContain('no video');
  });

  it('says "upload overdue" once the server flags the night overdue', () => {
    const html = _gridCellHtml('20260914', 'RO0001', [cell({ pending: true, overdue: true })]);
    expect(html).toContain('>upload overdue<');
  });

  it('says "no video" for a camera that never delivers clips', () => {
    // gmnro17 runs plots-only Pis: "clip pending" promised an upload that was
    // never coming, on every detection, forever.
    const html = _gridCellHtml('20260914', 'RO0001', [cell({ pending: true, no_clip: true })]);
    expect(html).toContain('>no video<');
    expect(html).toContain('no video from this camera');
    expect(html).not.toContain('not uploaded yet');
  });

  it('says "removed" once retention removed the video', () => {
    const html = _gridCellHtml('20260906', 'RO0001', [cell({ pending: true, pruned: true })]);
    expect(html).toContain('>removed<');
    expect(html).toContain('video removed after 14 days');
    expect(html).not.toContain('not uploaded yet');
  });

  it('still says "no img" for a non-pending chunk with no stack', () => {
    const html = _gridCellHtml('20260914', 'RO0001', [
      cell({ pending: false, filename: 'RO0001_20260914_201102_color.mkv' }),
    ]);
    expect(html).toContain('>no img<');
  });
});

describe('grid links', () => {
  it('opens a camera in the Video DB on the night and window', () => {
    expect(gridCamUrl('gmnro07', 'RO000W', '20260920', { from: '15:32', to: '15:37' }))
      .toBe('/station/gmnro07/videodb?cam=RO000W&date=20260920&from=15:32&to=15:37');
    expect(gridCamUrl('gmnro07', 'RO000W', null)).toBe('/station/gmnro07/videodb?cam=RO000W');
  });
  it('opens a station on the night', () => {
    expect(gridStationUrl('gmnro07', '20260920')).toBe('/station/gmnro07/rms?date=20260920');
  });
});

describe('_gridClampToWindow (no footage outside the capture window)', () => {
  // footage 19:30 → 03:40 UTC, in the sliders' noon-anchored minutes
  const win = { lo: 19 * 60 + 30 - 720, hi: 3 * 60 + 40 + 720 };
  const hm = (h, m) => h * 60 + m;

  it('keeps a time inside the window', () => {
    expect(_gridClampToWindow(hm(22, 15), win)).toBe(hm(22, 15));
    expect(_gridClampToWindow(hm(1, 0), win)).toBe(hm(1, 0));
  });
  it('pulls an evening time before the start up to it', () => {
    expect(_gridClampToWindow(hm(18, 0), win)).toBe(hm(19, 30));
  });
  it('pulls a daytime morning back to the end, leaving room for the span', () => {
    expect(_gridClampToWindow(hm(10, 0), win)).toBe(hm(3, 40));
    expect(_gridClampToWindow(hm(10, 0), win, 5)).toBe(hm(3, 35));
  });
  it('leaves the time alone without a window or a time', () => {
    expect(_gridClampToWindow(hm(10, 0), null)).toBe(hm(10, 0));
    expect(_gridClampToWindow(null, win)).toBeNull();
  });
});

describe('_gridThumbImgFailed', () => {
  it('replaces a failed grid thumbnail with a "not uploaded yet" tile', () => {
    document.body.innerHTML = '<div class="grid-thumb" data-fn="a.mkv"><img src="/stack/x.webp"></div>';
    _gridThumbImgFailed(document.querySelector('img'));
    const thumb = document.querySelector('.grid-thumb');
    expect(thumb.querySelector('img')).toBeNull();
    expect(thumb.querySelector('.grid-thumb-noimg').textContent).toBe('not uploaded yet');
    expect(thumb.dataset.fn).toBe('a.mkv');
  });

  it('leaves images outside a grid thumb alone', () => {
    document.body.innerHTML = '<div><img src="/logo.webp"></div>';
    _gridThumbImgFailed(document.querySelector('img'));
    expect(document.querySelector('img')).not.toBeNull();
  });
});

describe('_gridCellHtml hidden detections', () => {
  const cell = (extra) => ({
    host_key: 'gmnro17', cam: 'RO0001', meteor_time: '2026-09-14T20:11:02',
    filename: 'RO0001_20260914_201000_color.mkv', stack: null, ...extra,
  });

  it('leaves a hidden detection out unless Show hidden is on, then dims it', () => {
    const hidden = cell({ hidden: true, hidden_by: 'operator', hidden_at: '2026-11-22T10:00:00', meteor_time: '2026-09-14T20:12:00' });
    expect(_gridCellHtml('20260914', 'RO0001', [hidden], false)).toContain('grid-cell-empty');
    const off = _gridCellHtml('20260914', 'RO0001', [hidden, cell()], false);
    expect(off.match(/grid-thumb[ "]/g).length).toBe(1);
    const on = _gridCellHtml('20260914', 'RO0001', [hidden, cell()], true);
    expect(on).toContain('hd-hidden');
    expect(on).toContain('Hidden by operator, 2026-11-22');
  });

  describe('the Hide button on each thumb', () => {
    afterEach(() => { state.AUTH_USER = null; state.CAN_HIDE = false; });

    it('is on every detection thumb for a session that may hide, Unhide on a hidden one', () => {
      state.AUTH_USER = 'operator'; state.CAN_HIDE = true;
      const html = _gridCellHtml('20260914', 'RO0001',
        [cell(), cell({ meteor_time: '2026-09-14T20:12:00', hidden: true })], true);
      document.body.innerHTML = `<table><tr>${html}</tr></table>`;
      const btns = [...document.querySelectorAll('.grid-thumb .hd-btn')];
      expect(btns.map(b => b.textContent)).toEqual(['Hide', 'Unhide']);
      expect(btns.map(b => b.dataset.hd)).toEqual(['hide', 'unhide']);
    });

    it('also on a pending detection (no clip yet), never for anonymous visitors or magic links', () => {
      state.AUTH_USER = 'operator'; state.CAN_HIDE = true;
      expect(_gridCellHtml('20260914', 'RO0001', [cell({ pending: true, filename: null })], false)).toContain('hd-btn');
      state.CAN_HIDE = false;   // a magic-link session
      expect(_gridCellHtml('20260914', 'RO0001', [cell()], false)).not.toContain('hd-btn');
      state.AUTH_USER = null;
      expect(_gridCellHtml('20260914', 'RO0001', [cell()], false)).not.toContain('hd-btn');
    });
  });
});
