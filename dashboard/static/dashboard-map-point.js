// "Which cameras saw this point": pure helpers for the Network overview's
// point picker. The overview supplies each camera's footprint ring (the one
// the map draws, from dashboard-overview.js `_cameraFootprintPts`); this
// module tests a clicked point against those rings over an altitude range and
// builds the Detections grid link for the cameras that match. No Leaflet, no
// DOM, so every piece is unit-testable.

const R_EARTH_KM = 6371;
const D2R = Math.PI / 180;

/** Default altitude range (km). Meteor heights are usually unknown when the
 *  picker is used; ablation runs roughly 30-120 km. */
export const PICK_ALT_MIN_KM = 30;
export const PICK_ALT_MAX_KM = 120;
export const PICK_ALT_STEP_KM = 10;

/** Ray-casting point-in-polygon on [lat, lon] pairs. Fine for footprints a
 *  few hundred km across, far from the poles and the antimeridian. */
export function pointInRing(lat, lon, ring) {
  if (!Array.isArray(ring) || ring.length < 3) return false;
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [yi, xi] = ring[i];
    const [yj, xj] = ring[j];
    if ((yi > lat) !== (yj > lat)
        && lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) {
      inside = !inside;
    }
  }
  return inside;
}

/** Heights to test for [lo, hi] km: every `step` km from `lo`, plus `hi`.
 *  A single height (lo === hi) is just that height. */
export function altitudeSamples(lo, hi, step = PICK_ALT_STEP_KM) {
  const a = Math.min(lo, hi);
  const b = Math.max(lo, hi);
  if (!(step > 0) || a === b) return [a];
  const out = [];
  for (let h = a; h < b; h += step) out.push(h);
  out.push(b);
  return out;
}

/** Great-circle distance (km) between two lat/lon points. */
export function groundDistanceKm(lat1, lon1, lat2, lon2) {
  const dLat = (lat2 - lat1) * D2R;
  const dLon = (lon2 - lon1) * D2R;
  const a = Math.sin(dLat / 2) ** 2
    + Math.cos(lat1 * D2R) * Math.cos(lat2 * D2R) * Math.sin(dLon / 2) ** 2;
  return 2 * R_EARTH_KM * Math.asin(Math.min(1, Math.sqrt(a)));
}

/** Elevation angle (deg) of a point `heightKm` above the ground `distKm` away
 *  (great-circle) from an observer at sea level, on a spherical Earth. */
export function elevationDeg(distKm, heightKm) {
  const c = distKm / R_EARTH_KM;
  const r = R_EARTH_KM + heightKm;
  return Math.atan2(r * Math.cos(c) - R_EARTH_KM, r * Math.sin(c)) / D2R;
}

/** Cameras whose footprint contains (lat, lon) at any of `heights`.
 *
 *  `cameras`: [{ key, cam, label, lat, lon, footprintAt(H) -> ring | null }],
 *  in the order to list them (station-number order). Returns one entry per
 *  matching camera with the heights it saw the point at, its ground distance,
 *  and the elevation at the middle of those heights. */
export function camerasSeeingPoint(lat, lon, heights, cameras) {
  const out = [];
  for (const c of cameras || []) {
    const seen = [];
    for (const h of heights) {
      const ring = c.footprintAt(h);
      if (ring && pointInRing(lat, lon, ring)) seen.push(h);
    }
    if (!seen.length) continue;
    const distKm = groundDistanceKm(c.lat, c.lon, lat, lon);
    const minH = Math.min(...seen);
    const maxH = Math.max(...seen);
    out.push({
      key: c.key, cam: c.cam, label: c.label,
      minH, maxH, distKm,
      elevDeg: elevationDeg(distKm, (minH + maxH) / 2),
    });
  }
  return out;
}

/** Matches grouped by station, keeping their order: [{ key, label, cams }]. */
export function groupByStation(matches) {
  const groups = [];
  const byKey = new Map();
  for (const m of matches || []) {
    let g = byKey.get(m.key);
    if (!g) {
      g = { key: m.key, label: m.label, cams: [] };
      byKey.set(m.key, g);
      groups.push(g);
    }
    g.cams.push(m);
  }
  return groups;
}

/** "gmnro10 · Roit" (same format as the Detections grid rows). */
export function stationLabel(key, label) {
  if (key && label && key !== label) return `${key} · ${label}`;
  return key || label || '';
}

const DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/;
const HHMM_RE = /^([01]\d|2[0-3]):?([0-5]\d)$/;

/** "HH:MM" (or "HHMM") -> minutes of day, else null. */
export function parseHHMM(str) {
  const m = HHMM_RE.exec(String(str || '').trim());
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
}

function _hhmm(min) {
  const m = ((min % 1440) + 1440) % 1440;
  return `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
}

/** RMS night (YYYYMMDD of the evening it starts) for a UTC date and time:
 *  nights run noon to noon UTC, so 02:00 on the 19th is the night of the 18th. */
export function nightForUtc(dateStr, hhmm) {
  const d = DATE_RE.exec(String(dateStr || ''));
  const min = parseHHMM(hhmm);
  if (!d || min == null) return null;
  const t = new Date(Date.UTC(Number(d[1]), Number(d[2]) - 1, Number(d[3])));
  if (Number.isNaN(t.getTime())) return null;
  if (min < 12 * 60) t.setUTCDate(t.getUTCDate() - 1);
  return t.toISOString().slice(0, 10).replace(/-/g, '');
}

/** The grid's footage window around an event time: starts `leadMin` before
 *  it and spans `spanMin` (the footage grid's 5-minute cap). */
export function gridWindowFor(hhmm, leadMin = 2, spanMin = 5) {
  const min = parseHHMM(hhmm);
  if (min == null) return null;
  return { gfrom: _hhmm(min - leadMin), gto: _hhmm(min - leadMin + spanMin) };
}

/** Detections grid link (All footage is the grid's default mode) for a
 *  night, window and camera list. */
export function gridUrlFor(night, win, cams) {
  const p = new URLSearchParams();
  p.set('tab', 'grid');
  if (night) p.set('date', night);
  if (win) {
    p.set('gfrom', win.gfrom);
    p.set('gto', win.gto);
  }
  if (cams && cams.length) p.set('cams', cams.join(','));
  return `/events?${p.toString().replace(/%3A/gi, ':').replace(/%2C/gi, ',')}`;
}

/** UTC "YYYY-MM-DD" and "HH:MM" for a Date (the picker's default time). */
export function utcDateTimeParts(now = new Date()) {
  const iso = now.toISOString();
  return { date: iso.slice(0, 10), time: iso.slice(11, 16) };
}
