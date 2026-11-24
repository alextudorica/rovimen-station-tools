import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

import {
  VideoModal, stackUrlFor, stackDownloadUrlFor, fmtMass,
} from '../../dashboard/static/dashboard-video-modal.js';
import { MultiDetModal } from '../../dashboard/static/dashboard-multi-det-modal.js';
import { state, deriveStackName } from '../../dashboard/static/dashboard-common.js';

// ── Operator-control hiding for anonymous visitors ────────────────────────────
//
// The shared VideoModal / MultiDetModal reveal operator-only actions (Download
// clip, Crop, loop/frame download, Show/Download stack, Download joint clip)
// based on the session identity in state.AUTH_USER. An anonymous public visitor
// (state.AUTH_USER == null) must get playback controls only; a logged-in
// operator keeps the full action set. These tests drive the real modules in
// jsdom and assert the computed inline display style on each control.

const _SINGLE_OPTS = {
  src: 'blob:test',
  title: 'clip',
  station: 'pub_station',
  camera: 'PUB001',
  date: '20260101',
  filename: 'PUB001_20260101_010101.mkv',
  // Operator features the caller always requests; the modal decides whether to
  // paint them based on auth state.
  download: { onClick: () => {} },
  stack: { url: '/media/v1/stack/PUB001/2026-01-01/PUB001_20260101_010101_stack.webp' },
  loopDlPath: '/loopclip/pub_station/PUB001/20260101/PUB001_20260101_010101.mkv',
};

function mount() {
  const el = document.createElement('div');
  document.body.appendChild(el);
  return el;
}

const OPERATOR_SELECTORS = [
  '.vm-download-btn',
  '.vm-crop-btn',
  '.vm-loop-dl-wrap',
  '.vm-stack-dl-btn',
  '.vm-stack-show-btn',
];

// ── Always-available BW FF stack ──────────────────────────────────────────────
//
// Every detection clip has a BW max-pixel meteor stack sharing its base name.
// The modal must derive and offer "Download stack" for it even when the caller
// passes no explicit stack URL, so events/archive/VDB modals all get the button.
// An explicit stack URL (color/archive stack, or an already-built one) wins.

describe('stack-URL builder', () => {
  it('derives the BW FF stack name from every clip filename form', () => {
    expect(deriveStackName('RO000V_20260730_214442_color.mkv'))
      .toBe('RO000V_20260730_214442_stack.webp');
    expect(deriveStackName('RO000V_20260730_214442_color.mp4'))
      .toBe('RO000V_20260730_214442_stack.webp');
    expect(deriveStackName('TST001_20260601_213000.mkv'))
      .toBe('TST001_20260601_213000_stack.webp');
    expect(deriveStackName('TST001_20260601_213000.mp4'))
      .toBe('TST001_20260601_213000_stack.webp');
  });

  it('returns null for an unrecognised / empty filename', () => {
    expect(deriveStackName('poster.jpg')).toBeNull();
    expect(deriveStackName('')).toBeNull();
    expect(deriveStackName(null)).toBeNull();
  });

  it('derives a /stack/ URL from clip identity when no stack URL is passed', () => {
    const url = stackUrlFor({
      station: 'test_station_1', camera: 'TST001', date: '20260601',
      filename: 'TST001_20260601_213000_color.mkv',
    });
    expect(url).toBe('/stack/test_station_1/TST001/20260601/TST001_20260601_213000_stack.webp');
  });

  it('honours an explicit stack.url over derivation', () => {
    const url = stackUrlFor({
      stack: { url: '/media/v1/stack/TST001/2026-06-01/x_stack.webp' },
      station: 'test_station_1', camera: 'TST001', date: '20260601',
      filename: 'TST001_20260601_213000_color.mkv',
    });
    expect(url).toBe('/media/v1/stack/TST001/2026-06-01/x_stack.webp');
  });

  it('returns null when the clip has no ROVIMEN identity to derive from', () => {
    expect(stackUrlFor({ filename: 'TST001_20260601_213000_color.mkv' })).toBeNull();
    expect(stackUrlFor({ station: 's', camera: 'c', date: '20260601' })).toBeNull();
    expect(stackUrlFor({})).toBeNull();
    expect(stackUrlFor(null)).toBeNull();
  });

  it('derives a branded /fullstack/ URL for the download (not the /stack thumbnail)', () => {
    const url = stackDownloadUrlFor({
      station: 'test_station_1', camera: 'TST001', date: '20260601',
      filename: 'TST001_20260601_213000_color.mkv',
    });
    expect(url).toBe('/fullstack/test_station_1/TST001/20260601/TST001_20260601_213000_stack.webp');
  });

  it('maps an explicit /stack/ thumbnail URL to /fullstack/ for the download', () => {
    const url = stackDownloadUrlFor({
      stack: { url: '/stack/test_station_1/TST001/20260601/TST001_20260601_213000_stack.webp' },
    });
    expect(url).toBe('/fullstack/test_station_1/TST001/20260601/TST001_20260601_213000_stack.webp');
  });

  it('honours an explicit stack.url (already-gated archive URL) for the download', () => {
    const url = stackDownloadUrlFor({
      stack: { url: '/api/archive/file/TST001/20260601/meteors/x_stack.webp' },
      station: 'test_station_1', camera: 'TST001', date: '20260601',
      filename: 'TST001_20260601_213000_color.mkv',
    });
    expect(url).toBe('/api/archive/file/TST001/20260601/meteors/x_stack.webp');
  });
});

describe('VideoModal always-available Download-stack button', () => {
  let mountEl;
  let prevUser;

  beforeEach(() => {
    prevUser = state.AUTH_USER;
    mountEl = mount();
    state.AUTH_USER = 'operator';   // operator: the stack button is not anon-gated
  });

  afterEach(() => {
    state.AUTH_USER = prevUser;
    mountEl.remove();
  });

  // Opening straight into the stack view referenced a download-button binding
  // that had moved into _wireDownloadButtons(): a ReferenceError that left the
  // player half-built. Only CI's lint caught it.
  it('opens directly in stack view without throwing and hides the clip downloads', () => {
    const modal = new VideoModal(mountEl, { trim: true });
    expect(() => modal.open({
      src: 'blob:test', title: 'clip', startInStack: true,
      station: 'test_station_1', camera: 'TST001', date: '20260601',
      filename: 'TST001_20260601_213000_color.mkv',
      stack: { url: '/stack/test_station_1/TST001/20260601/TST001_20260601_213000_stack.webp' },
    })).not.toThrow();
    expect(mountEl.querySelector('.vm-download-btn').style.display).toBe('none');
    expect(mountEl.querySelector('.vm-download-trim-btn').style.display).toBe('none');
    expect(mountEl.querySelector('.vm-backvideo-btn').style.display).not.toBe('none');
  });

  // The load-bearing case: an events/archive clip opened WITHOUT any stack URL.
  it('shows Download stack for a detection clip even with no color stack', () => {
    const modal = new VideoModal(mountEl, { trim: true });
    modal.open({
      src: 'blob:test', title: 'clip',
      station: 'test_station_1', camera: 'TST001', date: '20260601',
      filename: 'TST001_20260601_213000_color.mkv',
      // no stack, no colorStack — mirrors the events/archive modal path.
    });
    const stackDl = mountEl.querySelector('.vm-stack-dl-btn');
    expect(stackDl.style.display).not.toBe('none');
    // Download hands out the branded full-resolution stack (/fullstack), which
    // burns in the ROVIMEN/astromania overlay on demand — never the un-branded
    // /stack thumbnail used for the inline preview.
    expect(stackDl.getAttribute('href'))
      .toBe('/fullstack/test_station_1/TST001/20260601/TST001_20260601_213000_stack.webp');
    // Filename is the BW stack webp, not the clip name.
    expect(stackDl.getAttribute('download')).toBe('TST001_20260601_213000_stack.webp');
    // The inline preview still uses the fast /stack thumbnail.
    expect(mountEl.querySelector('.vm-stack-img').getAttribute('src'))
      .toBe('/stack/test_station_1/TST001/20260601/TST001_20260601_213000_stack.webp');
    // No color stack → the second button stays hidden.
    expect(mountEl.querySelector('.vm-color-stack-dl-btn').style.display).toBe('none');
    // Show stack toggle is available too.
    expect(mountEl.querySelector('.vm-stack-show-btn').style.display).not.toBe('none');
  });

  // The stack VIEW fills the modal: it must end on the full-res stack, not the
  // 480 px /stack thumbnail the grid and Events pass in (user report 2026-09-25).
  it('swaps the stack view to the full-res stack once it has loaded', async () => {
    const loads = [];
    class FakeImage {
      set src(v) { this._src = v; loads.push(v); queueMicrotask(() => this.onload?.()); }
      get src() { return this._src; }
    }
    vi.stubGlobal('Image', FakeImage);
    try {
      const modal = new VideoModal(mountEl, { trim: true });
      modal.open({
        src: 'blob:test', title: 'clip',
        station: 'gmnro04', camera: 'RO0014', date: '20260924',
        filename: 'RO0014_20260924_213311_color.mkv',
        stack: { url: '/stack/gmnro04/RO0014/20260924/RO0014_20260924_213311_stack.webp' },
      });
      const img = mountEl.querySelector('.vm-stack-img');
      expect(img.getAttribute('src')).toBe('/stack/gmnro04/RO0014/20260924/RO0014_20260924_213311_stack.webp');
      mountEl.querySelector('.vm-stack-show-btn').click();
      await Promise.resolve();
      expect(loads).toEqual(['/fullstack/gmnro04/RO0014/20260924/RO0014_20260924_213311_stack.webp']);
      expect(img.getAttribute('src')).toBe('/fullstack/gmnro04/RO0014/20260924/RO0014_20260924_213311_stack.webp');
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('offers the color stack IN ADDITION when the caller supplies one', () => {
    const modal = new VideoModal(mountEl, { trim: true });
    modal.open({
      src: 'blob:test', title: 'clip',
      station: 'test_station_1', camera: 'TST001', date: '20260601',
      filename: 'TST001_20260601_213000_color.mkv',
      colorStack: { url: '/fullstack/test_station_1/TST001/20260601/TST001_20260601_213000_color_stack.webp' },
    });
    // BW stack still present.
    expect(mountEl.querySelector('.vm-stack-dl-btn').style.display).not.toBe('none');
    // Color stack now present too.
    const colorDl = mountEl.querySelector('.vm-color-stack-dl-btn');
    expect(colorDl.style.display).not.toBe('none');
    expect(colorDl.getAttribute('href'))
      .toContain('_color_stack.webp');
  });

  it('hides the button only when there is no derivable identity', () => {
    const modal = new VideoModal(mountEl, { trim: false });
    modal.open({ src: 'blob:x', title: 'external clip' });  // GMN public clip: no identity
    expect(mountEl.querySelector('.vm-stack-dl-btn').style.display).toBe('none');
    expect(mountEl.querySelector('.vm-stack-show-btn').style.display).toBe('none');
  });
});

describe('VideoModal operator-control hiding', () => {
  let mountEl;
  let prevUser;

  beforeEach(() => {
    prevUser = state.AUTH_USER;
    mountEl = mount();
  });

  afterEach(() => {
    state.AUTH_USER = prevUser;
    mountEl.remove();
  });

  it('anonymous visitor: every operator control is hidden, playback remains', () => {
    state.AUTH_USER = null;
    const modal = new VideoModal(mountEl, { trim: true });
    modal.open({ ..._SINGLE_OPTS });
    for (const sel of OPERATOR_SELECTORS) {
      const btn = mountEl.querySelector(sel);
      expect(btn, sel).toBeTruthy();
      expect(btn.style.display, `${sel} must be hidden for anon`).toBe('none');
    }
    // Playback controls stay available.
    expect(mountEl.querySelector('.vm-btn-play').style.display).not.toBe('none');
    expect(mountEl.querySelector('.vm-trackbar')).toBeTruthy();
  });

  it('logged-in operator: download / crop / stack controls are shown', () => {
    state.AUTH_USER = 'operator';
    const modal = new VideoModal(mountEl, { trim: true });
    modal.open({ ..._SINGLE_OPTS });
    // Download clip + Crop + Show stack are visible for an operator.
    expect(mountEl.querySelector('.vm-download-btn').style.display).not.toBe('none');
    expect(mountEl.querySelector('.vm-crop-btn').style.display).not.toBe('none');
    expect(mountEl.querySelector('.vm-stack-show-btn').style.display).not.toBe('none');
  });
});

describe('MultiDetModal download-grid hiding', () => {
  let mountEl;
  let prevUser;

  beforeEach(() => {
    prevUser = state.AUTH_USER;
    mountEl = mount();
  });

  afterEach(() => {
    state.AUTH_USER = prevUser;
    mountEl.remove();
  });

  const _MULTI_OPTS = {
    title: 'event',
    videos: [
      { cam: 'PUB001', station: 'pub_station', date: '20260101',
        filename: 'PUB001_20260101_010101.mkv', url: 'blob:a', offset: 0,
        downloadUrl: '/shortclip/pub_station/PUB001/20260101/x.mkv' },
      { cam: 'PUB002', station: 'pub_station', date: '20260101',
        filename: 'PUB002_20260101_010101.mkv', url: 'blob:b', offset: 0,
        downloadUrl: '/shortclip/pub_station/PUB002/20260101/y.mkv' },
    ],
  };

  it('anonymous visitor: "Download joint clip" is hidden', () => {
    state.AUTH_USER = null;
    const modal = new MultiDetModal(mountEl);
    modal.open({ ..._MULTI_OPTS });
    const stitch = mountEl.querySelector('.mdm-stitch-btn');
    expect(stitch).toBeTruthy();
    expect(stitch.style.display).toBe('none');
  });

  it('logged-in operator: "Download joint clip" is shown', () => {
    state.AUTH_USER = 'operator';
    const modal = new MultiDetModal(mountEl);
    modal.open({ ..._MULTI_OPTS });
    const stitch = mountEl.querySelector('.mdm-stitch-btn');
    expect(stitch.style.display).not.toBe('none');
  });
});

// ── Multi-station detail panel (issue #947) ───────────────────────────────────
//
// A clip whose meteor was triangulated has a real orbit behind it. The panel
// under the video is where that reaches a viewer: single-station astrometry
// first, then the GMN solution. A meteor GMN has not published (the normal
// state for anything recent) must render the single-station block alone rather
// than a column of dashes.

const _TRAJECTORY = {
  trajectory_id: '20260115200001_test0',
  shower: 'PER',
  ra_geo_deg: 45.1,
  dec_geo_deg: 58.0,
  velocity_geo_km_s: 59.2,
  velocity_init_km_s: 60.4,
  velocity_avg_km_s: 58.1,
  velocity_hel_km_s: 41.3,
  altitude_begin_km: 102.0,
  altitude_peak_km: 96.3,
  altitude_end_km: 88.0,
  peak_absolute_magnitude: -2.1,
  mass_kg: 9.3e-5,
  convergence_angle_deg: 31.2,
  orbit_a_au: 2.346,
  orbit_e: 0.803,
  orbit_i_deg: 11.72,
  orbit_peri_deg: 282.47,
  orbit_node_deg: 158.24,
  orbit_q_au: 0.463,
  orbit_q_aph_au: 4.23,
  tisserand: 3.0,
  stations: ['RO000H', 'RO000M', 'MD0001'],
  station_count: 3,
};

const _DETECTION = {
  shower: 'PER',
  mag_apparent: -2.0,
  mag_absolute: -3.5,
  duration_s: 0.42,
  angular_velocity: 10.3,
  time_utc: '2026-01-15T20:00:01',
  camera: 'RO000H',
  station: 'Test Station',
  ra_beg: 91.0, dec_beg: 22.0, ra_end: 92.0, dec_end: 23.0,
  azim_beg: 130.0, elev_beg: 45.0, azim_end: 131.0, elev_end: 46.0,
  ra_radiant: 200.0, dec_radiant: 55.0, radiant_elev: 45.0,
  solar_lon: 294.5,
};

describe('VideoModal multi-station detail panel', () => {
  let mountEl;
  let prevUser;

  beforeEach(() => {
    prevUser = state.AUTH_USER;
    mountEl = mount();
    state.AUTH_USER = 'operator';
  });

  afterEach(() => {
    state.AUTH_USER = prevUser;
    mountEl.remove();
  });

  function openWith(detection) {
    const modal = new VideoModal(mountEl, { trim: true });
    modal.open({ ..._SINGLE_OPTS, detection });
    return mountEl.querySelector('.vm-info-grid').textContent;
  }

  it('renders the GMN solution under a triangulated clip', () => {
    const text = openWith({ ..._DETECTION, trajectory: _TRAJECTORY });
    expect(text).toContain('Multi-station solution');
    expect(text).toContain('3 cameras');
    expect(text).toContain('RO000M');
    // Velocity trio, heliocentric velocity, heights, mass, orbit.
    expect(text).toContain('geo 59.2');
    expect(text).toContain('entry 60.4');
    expect(text).toContain('41.3 km/s');
    expect(text).toContain('102.0 → 96.3 → 88.0 km');
    expect(text).toContain('93.0 mg');
    expect(text).toContain('a 2.346 AU');
    expect(text).toContain('e 0.803');
    expect(text).toContain('31.2°');            // convergence angle
    expect(text).toContain('20260115200001_test0');
    // Single-station astrometry is still there, above it.
    expect(text).toContain('Az/Elev begin');
  });

  it('flags the orbit in the collapsed summary line', () => {
    const modal = new VideoModal(mountEl, { trim: true });
    modal.open({ ..._SINGLE_OPTS, detection: { ..._DETECTION, trajectory: _TRAJECTORY } });
    const summary = mountEl.querySelector('.vm-info-summary-text').textContent;
    expect(summary).toContain('orbit');
    // The entry speed, as on the cards; the geocentric 59.2 stays in the grid.
    expect(summary).toContain('entry 60.4 km/s');
    expect(summary).not.toContain('59.2');
  });

  it('omits the block entirely when the meteor was not triangulated', () => {
    const text = openWith({ ..._DETECTION, trajectory: null });
    expect(text).not.toContain('Multi-station solution');
    expect(text).not.toContain('Tisserand');
    // ...but the single-station detail is unaffected.
    expect(text).toContain('Shower');
    expect(text).toContain('Magnitude');
    const summary = mountEl.querySelector('.vm-info-summary-text').textContent;
    expect(summary).not.toContain('orbit');
  });

  it('drops individual trajectory fields GMN left blank', () => {
    // A solution with no mass estimate and no peak height must not render an
    // empty "Mass" row or a height string with a hole in it.
    const text = openWith({
      ..._DETECTION,
      trajectory: { ..._TRAJECTORY, mass_kg: null, altitude_peak_km: null },
    });
    expect(text).toContain('Multi-station solution');
    expect(text).not.toContain('Mass');
    expect(text).toContain('102.0 → 88.0 km (begin/end)');
  });
});

describe('photometric mass formatting', () => {
  it('picks a unit that keeps the number readable', () => {
    expect(fmtMass(2.5)).toBe('~2.50 kg');
    expect(fmtMass(0.0093)).toBe('~9.3 g');
    expect(fmtMass(9.3e-5)).toBe('~93.0 mg');
    expect(fmtMass(4.2e-7)).toBe('~420.0 µg');
  });

  it('returns null for a missing or nonsensical mass', () => {
    expect(fmtMass(null)).toBe(null);
    expect(fmtMass(undefined)).toBe(null);
    expect(fmtMass(0)).toBe(null);
    expect(fmtMass(-1)).toBe(null);
    expect(fmtMass(Infinity)).toBe(null);
  });
});

// ── Original download, trimmed download, open links ──────────────────────────
//
// "Download clip" used to hand out a trimmed libx264 re-encode. With an
// originalUrl it now fetches the original file, and the trim-aware cut becomes
// the separate "Download trimmed". Station / Video DB links appear whenever the
// clip's station, camera and night are known.

import { vmClipWindow, vmOpenLinks } from '../../dashboard/static/dashboard-video-modal.js';

describe('vmClipWindow / vmOpenLinks', () => {
  it('puts a window of a few minutes around the clip start + detection', () => {
    expect(vmClipWindow('RO000W_20260920_153210_color.mkv', 20)).toEqual({ from: '15:29', to: '15:36' });
    expect(vmClipWindow('RO000W_20260920_235900_color.mkv', 0)).toEqual({ from: '23:56', to: '00:02' });
    expect(vmClipWindow('nothing.mkv')).toEqual({});
  });

  it('builds station and Video DB links with the clip, normalising ISO nights', () => {
    const links = vmOpenLinks({
      station: 'gmnro07', camera: 'RO000W', date: '2026-09-20',
      filename: 'RO000W_20260920_153210_color.mkv', detOffset: 20,
    });
    expect(links.stationUrl).toBe('/station/gmnro07/rms?cam=RO000W&date=20260920&clip=RO000W_20260920_153210_color.mkv');
    expect(links.vdbUrl).toBe('/station/gmnro07/videodb?cam=RO000W&date=20260920&from=15:29&to=15:36&clip=RO000W_20260920_153210_color.mkv');
    expect(vmOpenLinks({ camera: 'RO000W', date: '20260920' })).toBeNull();
  });
});

describe('download buttons', () => {
  let savedUser;
  beforeEach(() => { savedUser = state.AUTH_USER; state.AUTH_USER = 'op'; });
  afterEach(() => { state.AUTH_USER = savedUser; });

  const q = (m, sel) => m._el.querySelector(sel);

  it('offers the original and the trimmed cut when both are known', () => {
    const m = new VideoModal(mount(), { trim: true });
    const cut = vi.fn();
    m.open({ ..._SINGLE_OPTS, download: { originalUrl: '/download/x/C/20260101/f.mkv', onClick: cut } });
    expect(q(m, '.vm-download-btn').style.display).toBe('');
    expect(q(m, '.vm-download-trim-btn').style.display).toBe('');
    q(m, '.vm-download-trim-btn').click();
    expect(cut).toHaveBeenCalledWith(q(m, '.vm-download-trim-btn'));
    m.close();
  });

  it('keeps the cut on the main button without an original', () => {
    const m = new VideoModal(mount(), { trim: true });
    const cut = vi.fn();
    m.open({ ..._SINGLE_OPTS, download: { onClick: cut } });
    expect(q(m, '.vm-download-trim-btn').style.display).toBe('none');
    q(m, '.vm-download-btn').click();
    expect(cut).toHaveBeenCalled();
    m.close();
  });

  it('a dragged selection moves onto the main button, the whole clip onto the second', () => {
    const m = new VideoModal(mount(), { trim: true });
    const cut = vi.fn();
    m.open({ ..._SINGLE_OPTS, download: { originalUrl: '/download/x/C/20260101/f.mkv', onClick: cut } });
    expect(q(m, '.vm-download-btn').textContent).toContain('Download clip');
    // What a handle drag leaves behind: the viewer's own sub-selection.
    m._trim.duration = 20;
    m._trim.touched = true;
    m.updateTrim({ start: 4, end: 9.5 });
    expect(q(m, '.vm-download-btn').textContent).toContain('Download selection (5.5 s)');
    expect(q(m, '.vm-download-trim-btn').textContent).toContain('Download whole clip');
    q(m, '.vm-download-btn').click();
    expect(cut).toHaveBeenCalledWith(q(m, '.vm-download-btn'));
    // Dragged back to the whole clip: the buttons return to their defaults.
    m.updateTrim({ start: 0, end: 20 });
    expect(q(m, '.vm-download-btn').textContent).toContain('Download clip');
    expect(q(m, '.vm-download-trim-btn').textContent).toContain('Download trimmed');
    m.close();
  });

  it('a preset window alone does not take over the main button', () => {
    const m = new VideoModal(mount(), { trim: true });
    m.open({ ..._SINGLE_OPTS, trimStart: 3, trimEnd: 8,
             download: { originalUrl: '/download/x/C/20260101/f.mkv', onClick: () => {} } });
    expect(q(m, '.vm-download-btn').textContent).toContain('Download clip');
    m.close();
  });

  it('fetches the whole clip for "Download clip"', async () => {
    const m = new VideoModal(mount(), { trim: true });
    const fetchSpy = vi.fn(async () => ({
      ok: true, status: 200, blob: async () => new Blob(['x']),
      headers: { get: h => (h === 'content-disposition' ? 'attachment; filename="f.mkv"' : 'video/x-matroska') },
    }));
    vi.stubGlobal('fetch', fetchSpy);
    if (!URL.createObjectURL) URL.createObjectURL = () => 'blob:x';
    if (!URL.revokeObjectURL) URL.revokeObjectURL = () => {};
    m.open({ ..._SINGLE_OPTS, download: { originalUrl: '/download/x/C/20260101/f.mkv', onClick: () => {} } });
    await m._downloadOriginal();
    expect(fetchSpy).toHaveBeenCalledWith('/download/x/C/20260101/f.mkv', expect.objectContaining({ signal: expect.anything() }));
    vi.unstubAllGlobals();
    m.close();
  });

  it('shows the open links for an operator and hides them for an anonymous visitor', () => {
    const m = new VideoModal(mount(), { trim: true });
    m.open(_SINGLE_OPTS);
    expect(q(m, '.vm-open-links').style.display).toBe('');
    expect(q(m, '.vm-open-vdb').getAttribute('href')).toContain('/station/pub_station/videodb?cam=PUB001&date=20260101');
    m.close();
    state.AUTH_USER = null;
    m.open({ ..._SINGLE_OPTS, download: { originalUrl: '/x', onClick: () => {} } });
    expect(q(m, '.vm-open-links').style.display).toBe('none');
    expect(q(m, '.vm-download-btn').style.display).toBe('none');
    expect(q(m, '.vm-download-trim-btn').style.display).toBe('none');
    m.close();
  });
});

// ── Trim of the clip on screen ────────────────────────────────────────────────
//
// The modal reuses one <video>. Each open() used to add `once` listeners for
// loadedmetadata; a clip that never reached metadata (still "preparing", or
// skipped with prev/next before it loaded) left its listener behind, and that
// stale listener ran first on the NEXT clip: it set the trim end to the whole
// duration, so the new clip's own trim was lost.

describe('metadata listeners of an earlier clip', () => {
  function stubMedia(m, duration) {
    const v = m._el.querySelector('.vm-video-el');
    Object.defineProperty(v, 'duration', { configurable: true, get: () => duration });
    v.play = () => Promise.resolve();
    v.pause = () => {};
    v.load = () => {};
    return v;
  }

  it('prev/next before the first clip loaded keeps the second clip\'s trim', () => {
    const m = new VideoModal(mount(), { trim: true });
    const v = stubMedia(m, 20);
    m.open({ ..._SINGLE_OPTS, src: '/clip/a.mp4' });                         // never loads
    m.open({ ..._SINGLE_OPTS, src: '/clip/b.mp4', trimStart: 3, trimEnd: 8 });
    v.dispatchEvent(new Event('loadedmetadata'));
    expect(m._trim.start).toBe(3);
    expect(m._trim.end).toBe(8);
    expect(m._trim.duration).toBe(20);
    m.close();
  });

  it('a clip closed before it loaded does not touch the next one', () => {
    const m = new VideoModal(mount(), { trim: true });
    const v = stubMedia(m, 20);
    m.open({ ..._SINGLE_OPTS, src: '/clip/a.mp4' });
    m.close();
    m.open({ ..._SINGLE_OPTS, src: '/clip/b.mp4', trimStart: 3, trimEnd: 8 });
    v.dispatchEvent(new Event('loadedmetadata'));
    expect(m._trim.end).toBe(8);
    m.close();
  });

  it('still takes the duration from the clip that did load', () => {
    const m = new VideoModal(mount(), { trim: true });
    const v = stubMedia(m, 12.5);
    m.open({ ..._SINGLE_OPTS, src: '/clip/a.mp4' });
    v.dispatchEvent(new Event('loadedmetadata'));
    expect(m._trim.end).toBe(12.5);
    expect(m._trim.duration).toBe(12.5);
    m.close();
  });
});
