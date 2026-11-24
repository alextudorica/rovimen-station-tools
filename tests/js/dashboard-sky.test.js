import { describe, it, expect } from 'vitest';
import { jdFromDate, sun, solToDate, dayOfYear, moonPhase, moonPhaseName, altitude, nightView } from '../../dashboard/static/dashboard-sky.js';

const hours = (a, b) => Math.abs(a.getTime() - b.getTime()) / 3600000;

describe('sun and solar longitude', () => {
  it('puts the 2026 March equinox at 20 March 14:46 UTC (Sun longitude 0 of date)', () => {
    const s = sun(jdFromDate(new Date('2026-03-20T14:46:00Z')));
    expect(Math.abs(((s.lon + 180) % 360) - 180)).toBeLessThan(0.02);
  });

  it('turns a J2000 solar longitude into that year\'s date', () => {
    // The March equinox in J2000 longitude is 0 minus 26 years of precession.
    const eq = solToDate(360 - 0.3632, 2026);
    expect(hours(eq, new Date('2026-03-20T14:46:00Z'))).toBeLessThan(1);
    // Perseids, MDC peak at 139.70: night of 12-13 August 2026, not the
    // 9 August the old linear rule gave.
    const per = solToDate(139.70, 2026);
    expect(per.toISOString().slice(0, 10)).toMatch(/^2026-08-1[23]$/);
    // Orionids near 208: 21-22 October.
    expect(solToDate(208.0, 2026).toISOString().slice(0, 10)).toMatch(/^2026-10-2[12]$/);
    // Quadrantids at 283.15 fall in the first days of January.
    expect(solToDate(283.15, 2026).toISOString().slice(0, 10)).toMatch(/^2026-01-0[34]$/);
  });

  it('day of year starts at 0 on 1 January', () => {
    expect(dayOfYear(new Date('2026-01-01T12:00:00Z'))).toBeCloseTo(0.5, 6);
  });
});

describe('moon phase', () => {
  it('matches known phases', () => {
    const full = moonPhase(jdFromDate(new Date('2024-01-25T17:54:00Z')));
    expect(full.fraction).toBeGreaterThan(0.995);
    const nm = moonPhase(jdFromDate(new Date('2024-04-08T18:21:00Z'))); // eclipse new moon
    expect(nm.fraction).toBeLessThan(0.005);
    const fq = moonPhase(jdFromDate(new Date('2024-01-18T03:53:00Z')));
    expect(fq.fraction).toBeGreaterThan(0.46);
    expect(fq.fraction).toBeLessThan(0.54);
    expect(fq.waxing).toBe(true);
    expect(moonPhaseName(fq)).toBe('first quarter');
    expect(moonPhaseName(full)).toBe('Full Moon');
  });
});

describe('altitude and the night view', () => {
  const site = { lat: 45.94, lon: 24.97 };

  it('Polaris stands at about the latitude all night', () => {
    for (const h of [0, 6, 12, 18]) {
      const jd = jdFromDate(new Date(Date.UTC(2026, 9, 21, h)));
      // Polaris is 0.74 deg from the pole, so it swings that far around the latitude.
      expect(Math.abs(altitude(37.95, 89.26, jd, site.lat, site.lon) - site.lat)).toBeLessThan(0.8);
    }
  });

  it('the Perseids radiant from Romania: dark from late evening, best before dawn', () => {
    const v = nightView(47.77, 57.79, new Date('2026-08-12T22:00:00Z'), site);
    expect(v.dark).not.toBeNull();
    expect(v.good).not.toBeNull();
    // highest in the dark at the end of the night, 60-80 deg up
    expect(v.best.alt).toBeGreaterThan(60);
    expect(v.best.at.getUTCHours()).toBeLessThan(3);
    // dark (Sun below -12) runs roughly 19:00 to 01:30 UTC in mid-August
    expect(v.dark.from.getUTCHours()).toBeGreaterThanOrEqual(18);
  });

  it('a southern radiant that never rises far: no good window', () => {
    const v = nightView(100, -70, new Date('2026-01-15T22:00:00Z'), site);
    expect(v.good).toBeNull();
    expect(v.best.alt).toBeLessThan(0);
  });
});
