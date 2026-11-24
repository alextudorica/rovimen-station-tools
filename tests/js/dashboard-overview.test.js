import { describe, it, expect, beforeEach, beforeAll } from 'vitest';

import { state } from '../../dashboard/static/dashboard-common.js';

// dashboard-overview.js instantiates a shared VideoModal at module load,
// which needs a real mount element. Provide it, then dynamic-import so the
// module side effects don't throw in jsdom. `ctx` is populated in beforeAll,
// so tests must read ctx.<fn> inside the test body (not at describe time).
let ctx;
beforeAll(async () => {
  const mount = document.createElement('div');
  mount.id = 'gmn-clip-modal-container';
  document.body.appendChild(mount);
  ctx = await import('../../dashboard/static/dashboard-overview.js');
});

describe('state.selectedStations', () => {
  it('is initialised as an empty Set', () => {
    expect(state.selectedStations).toBeInstanceOf(Set);
  });
});

describe('_toggleInSet', () => {
  beforeEach(() => {
    state.selectedStations.clear();
  });

  it('adds a missing key and reports it as now-selected', () => {
    const set = new Set();
    expect(ctx._toggleInSet(set, 'gmn0004')).toBe(true);
    expect(set.has('gmn0004')).toBe(true);
  });

  it('removes a present key and reports it as now-deselected', () => {
    const set = new Set(['gmn0004']);
    expect(ctx._toggleInSet(set, 'gmn0004')).toBe(false);
    expect(set.has('gmn0004')).toBe(false);
  });

  it('toggles back and forth', () => {
    const set = new Set();
    expect(ctx._toggleInSet(set, 'gmnro03')).toBe(true);
    expect(ctx._toggleInSet(set, 'gmnro03')).toBe(false);
    expect(ctx._toggleInSet(set, 'gmnro03')).toBe(true);
    expect([...set]).toEqual(['gmnro03']);
  });

  it('handles multiple independent keys (multi-select)', () => {
    const set = new Set();
    ctx._toggleInSet(set, 'gmn0004');
    ctx._toggleInSet(set, 'gmnro03');
    ctx._toggleInSet(set, 'gmnro07');
    expect(set.size).toBe(3);
    ctx._toggleInSet(set, 'gmnro03'); // deselect the middle one
    expect([...set].sort()).toEqual(['gmn0004', 'gmnro07']);
  });
});

describe('_unionFovIds', () => {
  const ALL = [
    'gmn0004_RO000T', 'gmn0004_RO000U',
    'gmnro03_RO000H', 'gmnro03_RO000J', 'gmnro03_RO000L',
    'gmnro07_RO000W', 'gmnro07_RO000X',
  ];

  it('returns nothing for no selected stations', () => {
    expect(ctx._unionFovIds([], ALL)).toEqual([]);
    expect(ctx._unionFovIds(new Set(), ALL)).toEqual([]);
  });

  it('returns every FOV id for a single selected station', () => {
    expect(ctx._unionFovIds(['gmnro03'], ALL)).toEqual([
      'gmnro03_RO000H', 'gmnro03_RO000J', 'gmnro03_RO000L',
    ]);
  });

  it('returns the union across several selected stations', () => {
    const union = ctx._unionFovIds(['gmn0004', 'gmnro07'], ALL);
    expect(union.sort()).toEqual([
      'gmn0004_RO000T', 'gmn0004_RO000U',
      'gmnro07_RO000W', 'gmnro07_RO000X',
    ]);
  });

  it('accepts a Set of keys', () => {
    expect(ctx._unionFovIds(new Set(['gmn0004']), ALL)).toEqual([
      'gmn0004_RO000T', 'gmn0004_RO000U',
    ]);
  });

  it('does not match a host that is a prefix of another host code', () => {
    // 'gmn0004' must not pull in a hypothetical 'gmn00041_RO0001'
    const ids = ['gmn0004_RO000T', 'gmn00041_RO0001'];
    expect(ctx._unionFovIds(['gmn0004'], ids)).toEqual(['gmn0004_RO000T']);
  });
});

describe('_gmnSeedDate', () => {
  const YESTERDAY = '2026-08-24';

  it('opens on the newest night GMN has published', () => {
    expect(ctx._gmnSeedDate('2026-08-20', YESTERDAY)).toBe('2026-08-20');
  });

  it('falls back to yesterday when the backend has no answer', () => {
    // Cold cache: the backend reports null rather than guessing a night.
    expect(ctx._gmnSeedDate(null, YESTERDAY)).toBe(YESTERDAY);
    expect(ctx._gmnSeedDate(undefined, YESTERDAY)).toBe(YESTERDAY);
    expect(ctx._gmnSeedDate('', YESTERDAY)).toBe(YESTERDAY);
  });

  it('rejects anything that is not a YYYY-MM-DD night', () => {
    expect(ctx._gmnSeedDate('20260820', YESTERDAY)).toBe(YESTERDAY);
    expect(ctx._gmnSeedDate('2026-08-20T00:00:00Z', YESTERDAY)).toBe(YESTERDAY);
    expect(ctx._gmnSeedDate({ date: '2026-08-20' }, YESTERDAY)).toBe(YESTERDAY);
    expect(ctx._gmnSeedDate(20260820, YESTERDAY)).toBe(YESTERDAY);
  });
});

describe('_gmnEmptyNotice', () => {
  it('is neutral and offers the newest night when GMN has not published yet', () => {
    const notice = ctx._gmnEmptyNotice({
      date: '2026-08-24', events: [],
      latest_available_date: '2026-08-20', not_published_yet: true,
    });
    expect(notice.tone).not.toBe('warn');
    expect(notice.text).toContain('not published this night yet');
    expect(notice.text).toContain('2026-08-20');
    expect(notice.jumpDate).toBe('2026-08-20');
  });

  it('stays red with no shortcut when the night is published but empty for us', () => {
    const notice = ctx._gmnEmptyNotice({
      date: '2026-08-18', events: [],
      latest_available_date: '2026-08-20', not_published_yet: false,
    });
    expect(notice.tone).toBe('warn');
    expect(notice.text).toBe('No orbits for this night \u2014 try another date');
    expect(notice.jumpDate).toBeNull();
  });

  it('falls back to comparing dates when the flag is missing', () => {
    // A payload cached before the backend grew the flag.
    const notice = ctx._gmnEmptyNotice({
      date: '2026-08-23', events: [], latest_available_date: '2026-08-20',
    });
    expect(notice.jumpDate).toBe('2026-08-20');
  });

  it('never claims a night is unpublished when no newer night is known', () => {
    expect(ctx._gmnEmptyNotice({ date: '2026-08-24', events: [] }).tone).toBe('warn');
    expect(ctx._gmnEmptyNotice({
      date: '2026-08-24', not_published_yet: true, latest_available_date: null,
    }).tone).toBe('warn');
  });

  it('survives a missing payload without throwing', () => {
    expect(ctx._gmnEmptyNotice(undefined).tone).toBe('warn');
    expect(ctx._gmnEmptyNotice(null).jumpDate).toBeNull();
  });
});

describe('_nwWeatherBadge', () => {
  it('shows the overcast badge on a stale + overcast station', () => {
    const badge = ctx._nwWeatherBadge({ detectionsStale: true, weather: 'overcast' });
    expect(badge).not.toBeNull();
    expect(badge.icon).toBe('☁');   // ☁
    expect(badge.cls).toBe('overcast');
  });

  it('shows nothing when the station is not stale (even if flagged overcast)', () => {
    // weather is only meaningful alongside a stale feed; a non-stale station
    // must not get the badge.
    expect(ctx._nwWeatherBadge({ detectionsStale: false, weather: 'overcast' })).toBeNull();
  });

  it('shows nothing for a stale-but-clear station', () => {
    expect(ctx._nwWeatherBadge({ detectionsStale: true, weather: null })).toBeNull();
  });

  it('is null-safe on old payloads missing the fields', () => {
    // Pre-feature /api/overview responses carry neither field → no throw, no badge.
    expect(ctx._nwWeatherBadge({})).toBeNull();
    expect(ctx._nwWeatherBadge(undefined)).toBeNull();
    expect(ctx._nwWeatherBadge(null)).toBeNull();
  });

  it('ignores unknown weather values', () => {
    expect(ctx._nwWeatherBadge({ detectionsStale: true, weather: 'foggy' })).toBeNull();
  });
});

describe('_nwSegmentBadge', () => {
  it('flags a clip storm with the offending camera and its counts', () => {
    const badge = ctx._nwSegmentBadge({
      segmentAlarm: { RO000Y: 'storm' },
      cameras: [
        { code: 'RO000Y', segment: { short: 1543, stalls: 0, window_min: 60 } },
        { code: 'RO0010', segment: { short: 0, stalls: 0, window_min: 60 } },
      ],
    });
    expect(badge).not.toBeNull();
    expect(badge.label).toBe('clip storm');
    expect(badge.cls).toBe('storm');
    expect(badge.title).toContain('RO000Y');
    expect(badge.title).toContain('1543 back-to-back segments');
    // The healthy camera must not be named in the warning.
    expect(badge.title).not.toContain('RO0010');
  });

  it('flags stalls separately from a storm', () => {
    const badge = ctx._nwSegmentBadge({
      segmentAlarm: { RO001E: 'stalls' },
      cameras: [{ code: 'RO001E', segment: { short: 2, stalls: 123, window_min: 60 } }],
    });
    expect(badge.label).toBe('capture stalls');
    expect(badge.cls).toBe('stalls');
    expect(badge.title).toContain('123 gaps');
  });

  it('reports both when one camera storms and another stalls', () => {
    const badge = ctx._nwSegmentBadge({
      segmentAlarm: { RO000Y: 'storm', RO0010: 'stalls' },
      cameras: [
        { code: 'RO000Y', segment: { short: 900, stalls: 0 } },
        { code: 'RO0010', segment: { short: 0, stalls: 40 } },
      ],
    });
    expect(badge.label).toBe('clip storm + stalls');
    // A storm is the louder failure, so it drives the colour.
    expect(badge.cls).toBe('storm');
    expect(badge.title).toContain('RO000Y');
    expect(badge.title).toContain('RO0010');
  });

  it('reports both when a single camera does both at once', () => {
    const badge = ctx._nwSegmentBadge({
      segmentAlarm: { RO000Y: 'both' },
      cameras: [{ code: 'RO000Y', segment: { short: 900, stalls: 30 } }],
    });
    expect(badge.label).toBe('clip storm + stalls');
    expect(badge.cls).toBe('storm');
  });

  it('names the camera even when its counters did not come through', () => {
    const badge = ctx._nwSegmentBadge({
      segmentAlarm: { RO000Y: 'storm' },
      cameras: [{ code: 'RO000Y', segment: null }],
    });
    expect(badge.title).toContain('RO000Y: storm');
  });

  it('shows nothing for a healthy fleet station', () => {
    expect(ctx._nwSegmentBadge({
      segmentAlarm: {},
      cameras: [{ code: 'RO000Y', segment: { short: 0, stalls: 0, window_min: 60 } }],
    })).toBeNull();
  });

  it('is null-safe on old payloads missing the fields', () => {
    // A station bundle that predates segment health reports nothing → no
    // throw, no badge, and definitely no false alarm.
    expect(ctx._nwSegmentBadge({})).toBeNull();
    expect(ctx._nwSegmentBadge({ cameras: [{ code: 'RO000Y' }] })).toBeNull();
    expect(ctx._nwSegmentBadge(undefined)).toBeNull();
    expect(ctx._nwSegmentBadge(null)).toBeNull();
  });
});

describe('stationHoverHtml', () => {
  const info = { label: 'Roit', cameras: [{ code: 'RO000E' }, { code: 'RO000G' }] };

  it('shows cameras and both orbit counts with the first month', () => {
    const html = ctx.stationHoverHtml('gmnro10', info, {
      ready: true, history_complete: true,
      stations: { gmnro10: { orbits_30d: 412, orbits_all: 3070, first_month: '202606' } },
    });
    expect(html).toContain('Roit');
    expect(html).toContain('gmnro10');
    expect(html).toContain('2 cameras');
    expect(html).toContain('last 30 days: <b>412</b>');
    expect(html).toContain('all time: <b>3,070</b>');
    expect(html).toContain('since Jun 2026');
    expect(html).not.toContain('loading');
  });

  it('says loading before the server has counts', () => {
    const html = ctx.stationHoverHtml('gmnro10', info, { ready: false, stations: {} });
    expect(html).toContain('GMN orbits loading');
    expect(ctx.stationHoverHtml('gmnro10', info, null)).toContain('GMN orbits loading');
  });

  it('shows zero for a station without orbits and flags partial history', () => {
    const html = ctx.stationHoverHtml('gmnro22', { label: 'Ferigile', cameras: [{}] },
      { ready: true, history_complete: false, stations: {} });
    expect(html).toContain('1 camera<');
    expect(html).toContain('all time: <b>0</b>');
    expect(html).toContain('older years loading');
  });
});

// Fleet capture headline: counted from each camera's FF evidence
// (`capture` on /api/overview), never from service state. On 2026-11-04 at
// 10:00 UTC the old count read "59 cameras capturing" in daylight.
describe('captureSummary / captureHeadline', () => {
  const cam = (state, verdict, night = '20261103') => ({
    capture: { state, detail: '', last_night: verdict ? { night, n_ff: 4300, expected: 4346, verdict } : null },
  });
  const overview = (cams, status = 'active') => ({ s1: { status, cameras: cams } });

  it('counts recording, degraded and stalled at night', () => {
    const sum = ctx.captureSummary({
      ...overview([cam('recording'), cam('recording'), cam('degraded'), cam('stalled'), cam('offline')]),
      s2: { status: 'commissioning', cameras: [cam('recording')] },
    });
    expect(sum.total).toBe(5);
    expect(ctx.captureHeadline(sum))
      .toBe('3 of 5 cameras recording (1 degraded, 1 stalled, 1 not reporting)');
  });

  it('reports last night in daytime', () => {
    const sum = ctx.captureSummary(overview([
      cam('idle', 'full'), cam('idle', 'full'), cam('idle', 'partial'), cam('idle', 'none'), cam('offline'),
    ]));
    expect(ctx.captureHeadline(sum)).toBe(
      'Daytime: RMS records at night. Night of 2026-11-03: 2 of 5 cameras recorded a full night, 1 part of it, 1 not reporting');
  });

  it('is null-safe on payloads without capture', () => {
    const sum = ctx.captureSummary(overview([{ code: 'X' }]));
    expect(sum.notReporting).toBe(1);
    // Before the stations run the bundle that reports capture evidence.
    expect(ctx.captureHeadline(sum)).toBe('Daytime: RMS records at night. Capture state not reported yet.');
    expect(ctx.captureHeadline(ctx.captureSummary({}))).toBe('No cameras');
  });
});

describe('_nwCaptureDot', () => {
  it('maps live states and judges idle cameras by last night', () => {
    expect(ctx._nwCaptureDot({ state: 'recording' }).cls).toBe('svc-ok');
    expect(ctx._nwCaptureDot({ state: 'degraded' }).cls).toBe('svc-warn');
    expect(ctx._nwCaptureDot({ state: 'stalled' }).cls).toBe('svc-dead');
    expect(ctx._nwCaptureDot({ state: 'idle', last_night: { verdict: 'full' } }).cls).toBe('svc-ok');
    expect(ctx._nwCaptureDot({ state: 'idle', last_night: { verdict: 'none' } }).cls).toBe('svc-dead');
    expect(ctx._nwCaptureDot({ state: 'unknown' })).toBeNull();
    expect(ctx._nwCaptureDot(null)).toBeNull();
  });
});

describe('_nwSkyMarker', () => {
  it('marks every camera with a sky row: overcast, partly, clear', () => {
    expect(ctx._nwSkyMarker({ night: '20261103', cloud_fraction: 0.97, median_stars: 1 }).cls).toBe('overcast');
    expect(ctx._nwSkyMarker({ cloud_fraction: 0.5 }).cls).toBe('partly');
    const clear = ctx._nwSkyMarker({ night: '20261103', cloud_fraction: 0.05, median_stars: 42 });
    expect(clear.cls).toBe('clear');
    expect(clear.title).toBe('Night 2026-11-03: 5% of frames clouded, median 42 stars per frame');
  });

  it('shows nothing without data', () => {
    expect(ctx._nwSkyMarker(null)).toBeNull();
    expect(ctx._nwSkyMarker({ cloud_fraction: null })).toBeNull();
  });
});

describe('_cameraFootprintPts (shared by the map and "Cameras at a point")', () => {
  // A camera at (45 N, 25 E) looking north 45 deg up with a 90 x 50 deg
  // field: it sees heights from about 20 to 70 deg elevation.
  const cam = { code: 'RO000T', az: 0, alt: 45 };
  const pp = { fov_h: 90, fov_v: 50 };

  it('is null for a camera without pointing', () => {
    expect(ctx._cameraFootprintPts(45, 25, { code: 'X' }, null, 100)).toBeNull();
  });

  it('returns a ring of [lat, lon] pairs north of the station', () => {
    const fp = ctx._cameraFootprintPts(45, 25, cam, pp, 100);
    expect(fp.pts.length).toBeGreaterThan(100);
    expect(fp.pts.every(p => Array.isArray(p) && p.length === 2)).toBe(true);
    expect(fp.center[0]).toBeGreaterThan(45.5);
    expect(fp.center[1]).toBeCloseTo(25, 3);
  });

  it('feeds camerasSeeingPoint: a point 111 km north is seen at 100 km, not at 30 km', async () => {
    const mp = await import('../../dashboard/static/dashboard-map-point.js');
    const cams = [{
      key: 'gmnro04', cam: 'RO000T', label: 'Vâlcele', lat: 45, lon: 25,
      footprintAt: H => ctx._cameraFootprintPts(45, 25, cam, pp, H)?.pts || null,
    }];
    expect(mp.camerasSeeingPoint(46, 25, [100], cams).map(m => m.cam)).toEqual(['RO000T']);
    expect(mp.camerasSeeingPoint(46, 25, [30], cams)).toEqual([]);
    expect(mp.camerasSeeingPoint(44, 25, mp.altitudeSamples(30, 120), cams)).toEqual([]);
    const [m] = mp.camerasSeeingPoint(46, 25, mp.altitudeSamples(30, 120), cams);
    expect(m.minH).toBeGreaterThan(30);
    expect(m.maxH).toBe(120);
  });

  it('caps the ring at the distance where the height sits 10 deg up', () => {
    // Looking at the horizon, the uncapped ring would run off to ~1000 km.
    const low = ctx._cameraFootprintPts(45, 25, { code: 'L', az: 0, alt: 15 }, pp, 100);
    const farthest = Math.max(...low.pts.map(p => p[0] - 45)) * 111;
    expect(farthest).toBeLessThan(500);
  });
});
