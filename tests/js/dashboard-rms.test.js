import { describe, it, expect } from 'vitest';

import * as ctx from '../../dashboard/static/dashboard-rms.js';

describe('entrySpeedText', () => {
  const f = ctx.entrySpeedText;

  it('shows the entry speed, never the geocentric Vg', () => {
    // RO001F + RO001U, 2026-11-20 22:24:19 UTC: a real slow meteoroid whose
    // Vg of 4.7 km/s read as impossible on the card.
    expect(f({ velocity_init_km_s: 11.786, velocity_geo_km_s: 4.709 })).toBe('11.8 km/s');
  });
  it('is empty without an entry speed, even when Vg is known', () => {
    expect(f({ velocity_geo_km_s: 38.5 })).toBe('');
    expect(f({ velocity_init_km_s: null })).toBe('');
    expect(f(null)).toBe('');
  });
});

describe('_parseRmsPlotImageUrl', () => {
  const f = ctx._parseRmsPlotImageUrl;

  it('parses a valid plot_image URL', () => {
    const r = f('/api/rms/plot_image/gmn0004/RO000T/20260530/detection_map.png');
    expect(r).toEqual({
      host: 'gmn0004',
      cam: 'RO000T',
      date: '20260530',
      filename: 'detection_map.png',
    });
  });
  it('handles URL-encoded filenames', () => {
    const r = f('/api/rms/plot_image/gmn0004/RO000T/20260530/file%20name.png');
    expect(r).toEqual({
      host: 'gmn0004',
      cam: 'RO000T',
      date: '20260530',
      filename: 'file name.png',
    });
  });
  it('parses absolute URL', () => {
    const r = f('http://localhost/api/rms/plot_image/gmn0004/RO000T/20260530/map.png');
    expect(r).toEqual({
      host: 'gmn0004',
      cam: 'RO000T',
      date: '20260530',
      filename: 'map.png',
    });
  });
  it('returns null for non-plot URL', () => {
    expect(f('/api/rms/chunks/gmn0004/RO000T/20260530')).toBeNull();
  });
  it('returns null for truncated plot URL', () => {
    expect(f('/api/rms/plot_image/gmn0004/RO000T')).toBeNull();
  });
  it('returns null for empty string', () => {
    expect(f('')).toBeNull();
  });
  it('returns null for garbage', () => {
    expect(f('not-a-url-at-all')).toBeNull();
  });
});

describe('rmsStripDomain', () => {
  const f = ctx.rmsStripDomain;
  it('pads the detections to the hour', () => {
    // 16:20 and 04:10 UTC = 260 and 970 minutes after noon
    expect(f([260, 970])).toEqual({ lo: 180, hi: 1020 });
  });
  it('falls back to 16:00-05:00 UTC without detections', () => {
    expect(f([])).toEqual({ lo: 240, hi: 1020 });
  });
  it('keeps at least two hours and stays inside the day', () => {
    expect(f([10, 15])).toEqual({ lo: 0, hi: 120 });
  });
});

describe('rmsNightStats', () => {
  const f = ctx.rmsNightStats;
  const d = (t, mag, o = {}) => ({ time_utc: `2026-10-22T${t}Z`, mag_apparent: mag, ...o });
  it('counts meteors, the brightest, the busiest hour and the orbits', () => {
    expect(f([d('02:10:00', 1.2), d('02:40:00', -1.4, { trajectory: {} }), d('03:05:00', 0.4), d('22:00:00', null)]))
      .toEqual({ meteors: 4, brightest: -1.4, peakHour: '02', peakCount: 2, orbits: 1 });
  });
  it('has no brightest or peak without data', () => {
    expect(f([])).toEqual({ meteors: 0, brightest: null, peakHour: null, peakCount: 0, orbits: 0 });
  });
});

// Shower codes arrive with each station's pushed detection rows (free text),
// so the RMS tab must render them as text and never splice them into code.
const EVIL_SHOWER = '"><img src=x onerror="window.__xss=1">\');//';

function parse(html) {
  const div = document.createElement('div');
  div.innerHTML = html;
  return div;
}

describe('_rmsShowerButtonsHtml', () => {
  it('escapes the code and reads it back from data-shower in the handler', () => {
    const out = parse(ctx._rmsShowerButtonsHtml('h1', 'C1', ['all', EVIL_SHOWER],
      { [EVIL_SHOWER]: 2 }, new Set(['all'])));
    expect(out.querySelector('img')).toBeNull();
    const btn = [...out.querySelectorAll('button')].find(b => b.dataset.shower !== 'all');
    expect(btn.dataset.shower).toBe(EVIL_SHOWER);
    expect(btn.getAttribute('onclick')).toBe("rmsSelectShower('h1','C1',this.dataset.shower)");
  });

  it('lets rmsSelectShower toggle a code that is not a valid CSS selector', () => {
    const row = document.createElement('div');
    row.id = 'rms-shower-btns-h1-C1';
    row.innerHTML = ctx._rmsShowerButtonsHtml('h1', 'C1', ['all', EVIL_SHOWER],
      { [EVIL_SHOWER]: 2 }, new Set(['all']));
    document.body.appendChild(row);
    try {
      window.rmsSelectShower('h1', 'C1', EVIL_SHOWER);
      const btn = [...row.querySelectorAll('button')].find(b => b.dataset.shower === EVIL_SHOWER);
      expect(btn.classList.contains('active')).toBe(true);
    } finally {
      row.remove();
    }
  });
});
