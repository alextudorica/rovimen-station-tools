import { describe, it, expect } from 'vitest';
import {
  pointInRing, altitudeSamples, groundDistanceKm, elevationDeg, camerasSeeingPoint,
  groupByStation, stationLabel, parseHHMM, nightForUtc, gridWindowFor, gridUrlFor,
  utcDateTimeParts,
} from '../../dashboard/static/dashboard-map-point.js';

// A 1-degree square around (45.5, 25.5).
const SQUARE = [[45, 25], [45, 26], [46, 26], [46, 25]];

describe('pointInRing', () => {
  it('finds a point inside and rejects one outside', () => {
    expect(pointInRing(45.5, 25.5, SQUARE)).toBe(true);
    expect(pointInRing(47, 25.5, SQUARE)).toBe(false);
    expect(pointInRing(45.5, 27, SQUARE)).toBe(false);
  });

  it('handles a concave ring', () => {
    // An L shape: the notch at the top right is outside.
    const ring = [[0, 0], [0, 2], [1, 2], [1, 1], [2, 1], [2, 0]];
    expect(pointInRing(0.5, 1.5, ring)).toBe(true);
    expect(pointInRing(1.5, 1.5, ring)).toBe(false);
  });

  it('is false for a missing or degenerate ring', () => {
    expect(pointInRing(0, 0, null)).toBe(false);
    expect(pointInRing(0, 0, [[0, 0], [1, 1]])).toBe(false);
  });
});

describe('altitudeSamples', () => {
  it('steps 10 km from the low end and always includes the high end', () => {
    expect(altitudeSamples(30, 120)).toEqual([30, 40, 50, 60, 70, 80, 90, 100, 110, 120]);
    expect(altitudeSamples(30, 95)).toEqual([30, 40, 50, 60, 70, 80, 90, 95]);
  });

  it('accepts the bounds in either order', () => {
    expect(altitudeSamples(60, 40)).toEqual([40, 50, 60]);
  });

  it('is one height when both ends are equal', () => {
    expect(altitudeSamples(87, 87)).toEqual([87]);
  });
});

describe('geometry', () => {
  it('measures one degree of latitude as about 111 km', () => {
    expect(groundDistanceKm(45, 25, 46, 25)).toBeCloseTo(111.2, 0);
  });

  it('puts a point overhead at 90 deg and lower with distance', () => {
    expect(elevationDeg(0, 100)).toBeCloseTo(90, 5);
    const near = elevationDeg(100, 100);
    const far = elevationDeg(300, 100);
    expect(near).toBeGreaterThan(far);
    // Flat-Earth atan(100/100) is 45 deg; curvature lowers it slightly.
    expect(near).toBeGreaterThan(43);
    expect(near).toBeLessThan(45);
  });
});

describe('camerasSeeingPoint', () => {
  // Footprint grows with height: below 60 km it misses the test point.
  const grows = H => (H >= 60 ? SQUARE : [[45, 25], [45, 25.2], [45.2, 25.2], [45.2, 25]]);
  const cams = [
    { key: 'gmnro04', cam: 'RO000T', label: 'Vâlcele', lat: 45.0, lon: 25.0, footprintAt: grows },
    { key: 'gmnro04', cam: 'RO000U', label: 'Vâlcele', lat: 45.0, lon: 25.0, footprintAt: () => null },
    { key: 'gmnro10', cam: 'RO000E', label: 'Roit', lat: 46.0, lon: 26.0, footprintAt: () => SQUARE },
  ];

  it('lists cameras that contain the point at any sampled height', () => {
    const got = camerasSeeingPoint(45.5, 25.5, altitudeSamples(30, 120), cams);
    expect(got.map(m => m.cam)).toEqual(['RO000T', 'RO000E']);
    expect(got[0]).toMatchObject({ key: 'gmnro04', minH: 60, maxH: 120 });
    expect(got[1]).toMatchObject({ minH: 30, maxH: 120 });
  });

  it('reports distance and the elevation at the middle of the seen heights', () => {
    const [m] = camerasSeeingPoint(45.5, 25.5, [90], cams);
    expect(m.distKm).toBeCloseTo(groundDistanceKm(45, 25, 45.5, 25.5), 6);
    expect(m.elevDeg).toBeCloseTo(elevationDeg(m.distKm, 90), 6);
  });

  it('with a single height lists only cameras covering it there', () => {
    expect(camerasSeeingPoint(45.5, 25.5, [40], cams).map(m => m.cam)).toEqual(['RO000E']);
  });

  it('groups by station in list order', () => {
    const groups = groupByStation(camerasSeeingPoint(45.5, 25.5, [90], cams));
    expect(groups.map(g => [g.key, g.cams.length])).toEqual([['gmnro04', 1], ['gmnro10', 1]]);
    expect(stationLabel(groups[1].key, groups[1].label)).toBe('gmnro10 · Roit');
  });
});

describe('stationLabel', () => {
  it('falls back to whichever part is present', () => {
    expect(stationLabel('gmnro23', '')).toBe('gmnro23');
    expect(stationLabel('', 'Roit')).toBe('Roit');
    expect(stationLabel('gmnro23', 'gmnro23')).toBe('gmnro23');
  });
});

describe('time and night', () => {
  it('parses HH:MM and HHMM, rejects anything else', () => {
    expect(parseHHMM('04:48')).toBe(288);
    expect(parseHHMM('2333')).toBe(23 * 60 + 33);
    expect(parseHHMM('24:00')).toBeNull();
    expect(parseHHMM('4:48')).toBeNull();
    expect(parseHHMM('')).toBeNull();
  });

  it('puts a morning time on the previous evening\'s night', () => {
    expect(nightForUtc('2026-11-19', '04:48')).toBe('20261118');
    expect(nightForUtc('2026-11-19', '11:59')).toBe('20261118');
  });

  it('puts an evening time on the same date\'s night', () => {
    expect(nightForUtc('2026-11-18', '18:30')).toBe('20261118');
    expect(nightForUtc('2026-11-18', '12:00')).toBe('20261118');
  });

  it('crosses month and year boundaries', () => {
    expect(nightForUtc('2026-12-01', '02:00')).toBe('20261130');
    expect(nightForUtc('2027-01-01', '02:00')).toBe('20261231');
  });

  it('is null for a bad date or time', () => {
    expect(nightForUtc('', '02:00')).toBeNull();
    expect(nightForUtc('2026-11-19', 'x')).toBeNull();
  });

  it('opens a 5-minute window starting 2 minutes before the event', () => {
    expect(gridWindowFor('04:50')).toEqual({ gfrom: '04:48', gto: '04:53' });
  });

  it('wraps the window across midnight', () => {
    expect(gridWindowFor('00:01')).toEqual({ gfrom: '23:59', gto: '00:04' });
  });

  it('formats the picker default in UTC', () => {
    expect(utcDateTimeParts(new Date('2026-11-19T04:48:30Z'))).toEqual({ date: '2026-11-19', time: '04:48' });
  });
});

describe('gridUrlFor', () => {
  it('links the Detections grid with night, window and cameras', () => {
    const url = gridUrlFor('20261118', { gfrom: '04:48', gto: '04:53' }, ['RO000E', 'RO000G']);
    expect(url).toBe('/events?tab=grid&date=20261118&gfrom=04:48&gto=04:53&cams=RO000E,RO000G');
    const p = new URLSearchParams(url.split('?')[1]);
    expect(p.get('cams')).toBe('RO000E,RO000G');
  });

  it('omits the camera list when empty', () => {
    expect(gridUrlFor('20261118', null, [])).toBe('/events?tab=grid&date=20261118');
  });
});
