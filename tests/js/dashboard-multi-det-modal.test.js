import { describe, it, expect, beforeEach, afterEach } from 'vitest';

import {
  MultiDetModal, trackBoundaries, resizeTrackPair, stackGridGeometry,
} from '../../dashboard/static/dashboard-multi-det-modal.js';
import { state } from '../../dashboard/static/dashboard-common.js';

function mount() {
  const el = document.createElement('div');
  document.body.appendChild(el);
  return el;
}

// ── Resizable grid cells (issue #493) ─────────────────────────────────────────
//
// The camera grid shipped every cell the same size, and focus mode was the only
// escape — one big cell plus a fixed rail. Track boundaries are draggable now:
// weight moves between the two tracks either side of the boundary grabbed, and
// nothing beyond that pair moves.

describe('grid track geometry', () => {
  it('places boundaries at the cumulative track edges', () => {
    // 100px wide, 3px padding each side, one 3px gutter → 91px of content.
    // Even split: 45.5px each, boundary centre at 3 + 45.5 + 1.5.
    expect(trackBoundaries([1, 1], 100)).toEqual([50]);
  });

  it('follows uneven weights', () => {
    // 3:1 → the boundary sits three quarters across the content box.
    const [x] = trackBoundaries([3, 1], 100);
    expect(x).toBeCloseTo(3 + 91 * 0.75 + 1.5, 6);
  });

  it('returns one boundary per interior edge, never a trailing one', () => {
    expect(trackBoundaries([1, 1, 1], 400)).toHaveLength(2);
    expect(trackBoundaries([1], 400)).toHaveLength(0);
  });

  it('yields nothing when the grid has no measurable size', () => {
    // jsdom and a display:none modal both report 0 — must not produce NaN.
    expect(trackBoundaries([1, 1], 0)).toEqual([]);
    expect(trackBoundaries([1, 1], 4)).toEqual([]);
  });
});

describe('grid track resizing', () => {
  it('conserves the pair total and leaves other tracks untouched', () => {
    const out = resizeTrackPair([1, 1, 1], 0, 0.4);
    expect(out[0]).toBeCloseTo(1.4, 6);
    expect(out[1]).toBeCloseTo(0.6, 6);
    expect(out[2]).toBe(1);                       // third track undisturbed
    expect(out[0] + out[1]).toBeCloseTo(2, 6);
  });

  it('clamps so a cell can never be dragged out of existence', () => {
    const out = resizeTrackPair([1, 1], 0, 999);
    expect(out[1]).toBeGreaterThan(0);
    expect(out[0] + out[1]).toBeCloseTo(2, 6);
    const back = resizeTrackPair([1, 1], 0, -999);
    expect(back[0]).toBeGreaterThan(0);
    expect(back[0] + back[1]).toBeCloseTo(2, 6);
  });

  it('does not mutate the input', () => {
    const fr = [1, 1];
    resizeTrackPair(fr, 0, 0.5);
    expect(fr).toEqual([1, 1]);
  });
});

describe('MultiDetModal resizable cells', () => {
  let mountEl;
  let prevUser;

  beforeEach(() => {
    prevUser = state.AUTH_USER;
    state.AUTH_USER = 'operator';
    mountEl = mount();
  });

  afterEach(() => {
    state.AUTH_USER = prevUser;
    mountEl.remove();
  });

  function videos(n) {
    return Array.from({ length: n }, (_, i) => ({
      cam: `PUB00${i}`, station: 'pub_station', date: '20260101',
      filename: `PUB00${i}_20260101_010101.mkv`, url: `blob:${i}`, offset: 0,
      downloadUrl: `/shortclip/pub_station/PUB00${i}/20260101/x.mkv`,
    }));
  }

  function openWith(n) {
    const modal = new MultiDetModal(mountEl);
    modal.open({ title: 'event', videos: videos(n) });
    return modal;
  }

  it('offers one handle per interior boundary of the 2x2 grid', () => {
    openWith(4);
    expect(mountEl.querySelectorAll('.mdm-gutter-col')).toHaveLength(1);
    expect(mountEl.querySelectorAll('.mdm-gutter-row')).toHaveLength(1);
  });

  it('offers no row handle when a single row holds every camera', () => {
    openWith(2);
    expect(mountEl.querySelectorAll('.mdm-gutter-col')).toHaveLength(1);
    expect(mountEl.querySelectorAll('.mdm-gutter-row')).toHaveLength(0);
  });

  it('writes explicit fr tracks so the boundaries are addressable', () => {
    openWith(4);
    const grid = mountEl.querySelector('.mdm-grid');
    expect(grid.style.gridTemplateColumns).toBe('1fr 1fr');
    expect(grid.style.gridTemplateRows).toBe('1fr 1fr');
  });

  it('keeps a dragged layout across focus and back', () => {
    const modal = openWith(4);
    modal._colFr = [1.6, 0.4];
    modal._applyTracks();

    modal._focusVideo(0);
    // Focused layout owns the template, so its handles come down.
    expect(mountEl.querySelectorAll('.mdm-gutter')).toHaveLength(0);
    expect(mountEl.querySelector('.mdm-grid').style.gridTemplateColumns)
      .toBe('1fr 184px');

    modal._unfocus();
    // ...and the viewer's own sizing is what comes back, not an even grid.
    expect(mountEl.querySelector('.mdm-grid').style.gridTemplateColumns)
      .toBe('1.6fr 0.4fr');
    expect(mountEl.querySelectorAll('.mdm-gutter-col')).toHaveLength(1);
  });

  it('evens out a boundary on double-click', () => {
    const modal = openWith(4);
    modal._colFr = [1.7, 0.3];
    modal._onGutterReset({ preventDefault() {}, stopPropagation() {} }, 'col', 0);
    expect(modal._colFr[0]).toBeCloseTo(1, 6);
    expect(modal._colFr[1]).toBeCloseTo(1, 6);
  });

  it('drops the layout on close so the next event opens even', () => {
    const modal = openWith(4);
    modal._colFr = [1.6, 0.4];
    modal.close();
    expect(modal._colFr).toEqual([]);

    modal.open({ title: 'next event', videos: videos(4) });
    expect(mountEl.querySelector('.mdm-grid').style.gridTemplateColumns)
      .toBe('1fr 1fr');
  });
});

import { mdmTileDownloadUrl, mdmTileCaption, mdmTileTrimUrl } from '../../dashboard/static/dashboard-multi-det-modal.js';

describe('multi-camera tiles: caption and per-clip download', () => {
  const w = {
    station: 'gmnde00', cam: 'DE001C', date: '20260925',
    filename: 'DE001C_20260925_215007_215027_color.mkv', label: 'Johannismühle',
    downloadUrl: '/shortclip/gmnde00/DE001C/20260925/x.mkv',
  };
  it('downloads the original via /download/ when the clip is fully named', () => {
    expect(mdmTileDownloadUrl(w))
      .toBe('/download/gmnde00/DE001C/20260925/DE001C_20260925_215007_215027_color.mkv');
    expect(mdmTileDownloadUrl({ ...w, date: 'bad' })).toBe(w.downloadUrl);
    expect(mdmTileDownloadUrl(null)).toBeNull();
  });
  it('captions each tile with station and camera', () => {
    expect(mdmTileCaption(w)).toBe('Johannismühle · DE001C');
    expect(mdmTileCaption({ cam: 'DE001C', label: 'DE001C' })).toBe('DE001C');
  });
});

describe('mdmTileTrimUrl', () => {
  const w = { cam: 'DE001C', date: '20260925', filename: 'DE001C_20260925_215007_color.mkv', offset: 6 };

  it('cuts the selection around the clip\'s detection, like the joint clip', () => {
    // Selection -2 .. +3 s around a detection 6 s into the clip.
    expect(mdmTileTrimUrl(w, -2, 3))
      .toBe('/api/archive/shortclip/DE001C/20260925/DE001C_20260925_215007_color.mkv?ss=4.00&t=5.00');
  });

  it('never starts before the clip and keeps half a second at least', () => {
    expect(mdmTileTrimUrl({ ...w, offset: 1 }, -5, -4.9)).toMatch(/\?ss=0\.00&t=0\.50$/);
  });

  it('needs the camera, night and clip', () => {
    expect(mdmTileTrimUrl({ ...w, date: 'bad' }, -2, 3)).toBeNull();
    expect(mdmTileTrimUrl(null, -2, 3)).toBeNull();
  });
});

// ── Witnesses without a clip ──────────────────────────────────────────────────
//
// An event whose second station has no clip in the archive (gmnro05 (Bistrița)
// encodes its meteor clips on demand only, so its witnesses stay pending) used
// to open on one camera: the events page dropped the url-less witness, and the
// joint clip was that one camera. The witness now gets a tile of its own, and
// the joint clip is offered only when it joins two cameras or more.

describe('MultiDetModal tiles without a clip', () => {
  let mountEl;
  let prevUser;

  beforeEach(() => {
    prevUser = state.AUTH_USER;
    state.AUTH_USER = 'operator';
    mountEl = mount();
  });

  afterEach(() => {
    state.AUTH_USER = prevUser;
    mountEl.remove();
  });

  const clip = (cam, station = 'gmnro04') => ({
    cam, station, date: '20260927', label: station, offset: 8.4,
    filename: `${cam}_20260928_005438_color.mkv`, url: `blob:${cam}`,
    downloadUrl: `/shortclip/${station}/${cam}/20260927/x.mkv`,
    stackUrl: `/stack/${station}/${cam}/20260927/${cam}_stack.webp`,
  });
  const noClip = (cam, station = 'gmnro05') => ({
    cam, station, date: '20260927', label: 'Bistrița', offset: 0,
    filename: null, url: null,
    stackUrl: `/stack/${station}/${cam}/20260927/${cam}_20260928_005438_stack.webp`,
    unavailable: { label: 'clip pending', title: 'Detected — clip upload in progress' },
  });
  const stitchBtn = () => mountEl.querySelector('.mdm-stitch-btn');

  it('gives the witness a tile with its stack and status instead of a player', () => {
    new MultiDetModal(mountEl).open({ title: 'e', videos: [clip('RO000V'), noClip('RO0005')] });
    const cells = mountEl.querySelectorAll('.mdm-cell');
    expect(cells).toHaveLength(2);
    const cell = cells[1];
    expect(cell.classList.contains('mdm-cell-noclip')).toBe(true);
    expect(cell.querySelector('video').getAttribute('src')).toBeNull();
    expect(cell.querySelector('.mdm-stack-img').getAttribute('src'))
      .toBe('/stack/gmnro05/RO0005/20260927/RO0005_20260928_005438_stack.webp');
    const msg = cell.querySelector('.mdm-err-msg');
    expect(msg.textContent).toBe('clip pending');
    expect(msg.title).toBe('Detected — clip upload in progress');
    // No clip, nothing to download for it.
    expect(cell.querySelector('.mdm-cap-dl')).toBeNull();
    expect(cells[0].querySelector('.mdm-cap-dl')).not.toBeNull();
  });

  it('counts the clipless tile as loaded so the others can start', () => {
    const modal = new MultiDetModal(mountEl);
    modal.open({ title: 'e', videos: [clip('RO000V'), noClip('RO0005')] });
    expect(modal._metaDone).toBe(1);
    expect(modal._total).toBe(2);
  });

  it('opens an event with no clip at all without waiting forever', () => {
    const modal = new MultiDetModal(mountEl);
    modal.open({ title: 'e', videos: [noClip('RO0005'), noClip('RO0001', 'gmnro17')] });
    // Every tile settled: the loading status is gone.
    expect(mountEl.querySelector('.mdm-status').style.display).toBe('none');
    expect(stitchBtn().style.display).toBe('none');
  });

  it('offers no joint clip when only one of the cameras has a clip', () => {
    new MultiDetModal(mountEl).open({ title: 'e', videos: [clip('RO000V'), noClip('RO0005')] });
    expect(stitchBtn().style.display).toBe('none');
  });

  it('offers the joint clip of the cameras that have one', () => {
    new MultiDetModal(mountEl).open({
      title: 'e', videos: [clip('RO000V'), clip('RO0017', 'gmnro11'), noClip('RO0005')] });
    expect(stitchBtn().style.display).toBe('');
    expect(stitchBtn().title).toContain('the 2 cameras that have a clip');
  });

  it('keeps the one-clip joint download of a single-tile modal', () => {
    new MultiDetModal(mountEl).open({ title: 'e', videos: [clip('RO000V')] });
    expect(stitchBtn().style.display).toBe('');
  });

  it('keeps the joint clip hidden across stack mode for a one-clip event', () => {
    const modal = new MultiDetModal(mountEl);
    modal.open({ title: 'e', videos: [clip('RO000V'), noClip('RO0005')] });
    modal._setStackMode(true);
    modal._setStackMode(false);
    expect(stitchBtn().style.display).toBe('none');
  });

  it('joins only the tiles with a clip, and re-indexes the focused one', async () => {
    const prevFetch = globalThis.fetch;
    let body = null;
    globalThis.fetch = async (_url, init) => {
      body = JSON.parse(init.body);
      return { ok: false, status: 500, json: async () => ({ error: 'stub' }) };
    };
    const prevAlert = globalThis.alert;
    globalThis.alert = () => {};
    try {
      const modal = new MultiDetModal(mountEl);
      modal.open({
        title: 'e', videos: [clip('RO000V'), noClip('RO0005'), clip('RO0017', 'gmnro11')] });
      // Focus the third tile: it is the second clip the server sees.
      modal._focusedIdx = 2;
      await modal._stitchDownload();
      expect(body.videos.map(v => v.cam)).toEqual(['RO000V', 'RO0017']);
      expect(body.layout).toBe('focused');
      expect(body.focused_idx).toBe(1);

      // A focused tile without a clip joins the rest as a grid.
      modal._focusedIdx = 1;
      await modal._stitchDownload();
      expect(body.layout).toBe('grid');
      expect(body.focused_idx).toBe(-1);
    } finally {
      globalThis.fetch = prevFetch;
      globalThis.alert = prevAlert;
    }
  });
});

// ── Stack grid download ───────────────────────────────────────────────────────
//
// Stack mode can be saved as one image laid out like the on-screen grid.

describe('stackGridGeometry', () => {
  it('lays four cameras out 2x2 like the on-screen grid', () => {
    const g = stackGridGeometry(4, 100, 50, 10, 0, 5);
    expect(g.width).toBe(5 + 2 * 105);
    expect(g.height).toBe(5 + 2 * 65);
    expect(g.cells).toEqual([
      { x: 5, y: 5 }, { x: 110, y: 5 },
      { x: 5, y: 70 }, { x: 110, y: 70 },
    ]);
  });

  it('goes three wide past four cameras and leaves room for the title', () => {
    const g = stackGridGeometry(5, 100, 50, 10, 20, 5);
    expect(g.width).toBe(5 + 3 * 105);
    expect(g.height).toBe(20 + 5 + 2 * 65);
    expect(g.cells[3]).toEqual({ x: 5, y: 20 + 5 + 65 });
  });
});

describe('stack download button', () => {
  it('shows only in stack mode', () => {
    const modal = new MultiDetModal(mount());
    modal.open({ title: 't', videos: [
      { cam: 'RO000A', stackUrl: '/stack/a.jpg' }, { cam: 'RO000B', stackUrl: '/stack/b.jpg' },
    ] });
    const btn = modal._q('.mdm-stackdl-btn');
    expect(btn.style.display).toBe('none');
    modal._toggleStackMode();
    expect(btn.style.display).toBe('');
    modal._toggleStackMode();
    expect(btn.style.display).toBe('none');
    modal.close();
  });
});
