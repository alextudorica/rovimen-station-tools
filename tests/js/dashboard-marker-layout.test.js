import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import {
  layoutStationMarkers, nameBox, boxOverlap, NAME_SIDES,
} from '../../dashboard/static/dashboard-marker-layout.js';

// The fleet as /api/stations served it on 2026-11-23: gmnro11 (Tărâța),
// gmnro16 (Bacău) and gmnro26 (Buhoci) within 30 px at zoom 7, and
// gmnro17 / gmnro19 (Câmpenița) on identical coordinates.
const FLEET = JSON.parse(readFileSync(resolve(import.meta.dirname, '..', 'e2e', 'fixtures', 'map_stations.json'), 'utf8'));

const DOT = 14;
const GAP = 3;
const scaleAt = z => Math.min(2.4, Math.max(0.6, 2 ** ((z - 7) / 2)));

// Web Mercator container pixels at zoom z (Leaflet's EPSG:3857).
function project(lat, lon, z) {
  const size = 256 * 2 ** z;
  const s = Math.sin((lat * Math.PI) / 180);
  return { x: (size * (lon + 180)) / 360, y: size * (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) };
}

// The name label's size as Chrome draws it: 11 px monospace place name,
// from zoom 7 the 10 px station ID after it, 5 px padding each side and a
// 1 px border, 21 px tall; all scaled with the dot. "Vaslui gmnro02"
// measured 99 x 21 px at zoom 7.
function nameSize(st, z) {
  const s = scaleAt(z);
  let w = st.label.length * 6.6 + 12;
  if (z >= 7) w += 5 + st.key.length * 6;
  return { w: w * s, h: 21 * s };
}

function fleetItems(z, stations = FLEET) {
  return stations.map(st => ({ key: st.key, ...project(st.lat, st.lon, z), ...nameSize(st, z) }));
}

// Every pairwise conflict left after the layout: names over names, names
// over dots, dots closer than 1.5 diameters.
function conflicts(items, layout, dot) {
  const r = dot / 2;
  const placed = items.map(it => {
    const p = layout[it.key];
    const cx = it.x + p.dx;
    const cy = it.y + p.dy;
    return {
      key: it.key, cx, cy,
      dot: { x0: cx - r, y0: cy - r, x1: cx + r, y1: cy + r },
      name: it.w ? nameBox(p.side, cx, cy, r, it.w, it.h, GAP) : null,
    };
  });
  const out = [];
  for (const a of placed) {
    for (const b of placed) {
      if (a === b) continue;
      if (a.name && b.name && a.key < b.key && boxOverlap(a.name, b.name) > 0) out.push(`name ${a.key} x name ${b.key}`);
      if (a.name && boxOverlap(a.name, b.dot) > 0) out.push(`name ${a.key} x dot ${b.key}`);
      // Fanned dots end up 1.5 diameters apart, and never closer than 1.25
      // to a neighbour they were fanned towards.
      if (a.key < b.key && Math.hypot(a.cx - b.cx, a.cy - b.cy) < 1.25 * dot - 1e-6) out.push(`dot ${a.key} x dot ${b.key}`);
    }
  }
  return out;
}

describe.skip('layoutStationMarkers on the fleet', () => {
  // 7 is the zoom of the 2026-11-23 screenshot; below 7 the names drop the
  // IDs, below 6.25 some need a corner position; 8 and 9 still have the
  // Bacău pair closer than 1.5 dots; 5.5 is the last zoom with names.
  for (const z of [5.5, 5.75, 6, 6.5, 7, 7.5, 8, 9]) {
    it(`leaves no overlapping dots or names at zoom ${z}`, () => {
      const dot = DOT * scaleAt(z);
      const items = fleetItems(z);
      const layout = layoutStationMarkers(items, { dot, gap: GAP });
      expect(conflicts(items, layout, dot)).toEqual([]);
      for (const it of items) expect(layout[it.key].overlap).toBe(0);
    });
  }

  // Zoomed out the names hide (w = h = 0) and the whole fleet is a few dozen
  // px across: the dots still must not touch, so each stays clickable.
  for (const z of [3, 4, 5]) {
    it(`keeps every dot clear of the others at zoom ${z} with the names hidden`, () => {
      const dot = DOT * scaleAt(z);
      const items = fleetItems(z).map(it => ({ ...it, w: 0, h: 0 }));
      const layout = layoutStationMarkers(items, { dot, gap: GAP });
      expect(conflicts(items, layout, dot)).toEqual([]);
    });
  }

  it('separates the Bacău trio and the Câmpenița pair at zoom 7', () => {
    const dot = DOT * scaleAt(7);
    const keys = ['gmnro11', 'gmnro16', 'gmnro26', 'gmnro17', 'gmnro19'];
    const items = fleetItems(7, FLEET.filter(st => keys.includes(st.key)));
    const layout = layoutStationMarkers(items, { dot, gap: GAP });
    expect(conflicts(items, layout, dot)).toEqual([]);
    // Bacău and Buhoci (9 px apart) and the Câmpenița pair are fanned out;
    // Tărâța (27 px from Bacău) keeps its place and moves its name instead.
    for (const k of ['gmnro16', 'gmnro26', 'gmnro17', 'gmnro19']) {
      expect(Math.hypot(layout[k].dx, layout[k].dy)).toBeGreaterThan(1);
    }
    expect(layout.gmnro11).toMatchObject({ dx: 0, dy: 0 });
    expect(layout.gmnro11.side).not.toBe('right');
  });

  it('leaves stations with no close neighbour where they are, name on the right', () => {
    const z = 7;
    const dot = DOT * scaleAt(z);
    const items = fleetItems(z);
    const layout = layoutStationMarkers(items, { dot, gap: GAP });
    // Cahul, Roit, Sighetu Marmației and Cugir have no station within 70 px.
    for (const k of ['gmnmd01', 'gmnro10', 'gmnro15', 'gmnro24']) {
      expect(layout[k]).toEqual({ dx: 0, dy: 0, side: 'right', overlap: 0 });
    }
  });

  it('is deterministic and independent of the input order', () => {
    const dot = DOT * scaleAt(7);
    const items = fleetItems(7);
    const a = layoutStationMarkers(items, { dot, gap: GAP });
    const b = layoutStationMarkers([...items].reverse(), { dot, gap: GAP });
    const c = layoutStationMarkers(items, { dot, gap: GAP });
    expect(b).toEqual(a);
    expect(c).toEqual(a);
  });
});

describe('layoutStationMarkers fan-out', () => {
  it('leaves a single station untouched', () => {
    expect(layoutStationMarkers([{ key: 'a', x: 50, y: 50, w: 40, h: 10 }], { dot: 14 }))
      .toEqual({ a: { dx: 0, dy: 0, side: 'right', overlap: 0 } });
  });

  it('splits identical points 1.5 dots apart around the shared position, first key on top', () => {
    const out = layoutStationMarkers([
      { key: 'gmnro19', x: 100, y: 100 }, { key: 'gmnro17', x: 100, y: 100 },
    ], { dot: 14 });
    expect(out.gmnro17.dx).toBeCloseTo(0, 9);
    expect(out.gmnro17.dy).toBeCloseTo(-10.5, 9);
    expect(out.gmnro19.dx).toBeCloseTo(0, 9);
    expect(out.gmnro19.dy).toBeCloseTo(10.5, 9);
  });

  it('pushes a close pair apart along its own line, no further than needed', () => {
    const out = layoutStationMarkers([{ key: 'a', x: 0, y: 0 }, { key: 'b', x: 6, y: 8 }], { dot: 10 });
    const ax = out.a.dx;
    const ay = out.a.dy;
    const bx = 6 + out.b.dx;
    const by = 8 + out.b.dy;
    expect(Math.hypot(bx - ax, by - ay)).toBeCloseTo(15, 9);
    expect((ax + bx) / 2).toBeCloseTo(3, 9);
    expect((ay + by) / 2).toBeCloseTo(4, 9);
    // Same direction as before: b stays down-right of a.
    expect((bx - ax) / (by - ay)).toBeCloseTo(6 / 8, 9);
  });

  it('does not move a pair that is already 1.5 dots apart', () => {
    const out = layoutStationMarkers([{ key: 'a', x: 0, y: 0 }, { key: 'b', x: 21, y: 0 }], { dot: 14 });
    expect(out.a).toMatchObject({ dx: 0, dy: 0 });
    expect(out.b).toMatchObject({ dx: 0, dy: 0 });
  });

  it('spreads three identical points evenly', () => {
    const items = ['a', 'b', 'c'].map(key => ({ key, x: 0, y: 0 }));
    const out = layoutStationMarkers(items, { dot: 10 });
    const pts = items.map(it => [out[it.key].dx, out[it.key].dy]);
    for (let i = 0; i < 3; i++) {
      const [x1, y1] = pts[i];
      const [x2, y2] = pts[(i + 1) % 3];
      expect(Math.hypot(x2 - x1, y2 - y1)).toBeCloseTo(15, 9);
    }
  });

  it('merges a fanned cluster with a dot it was pushed into', () => {
    // a and b are identical; fanned vertically they land on c, so all three
    // end up one cluster.
    const items = [{ key: 'a', x: 0, y: 0 }, { key: 'b', x: 0, y: 0 }, { key: 'c', x: 0, y: 18 }];
    const out = layoutStationMarkers(items, { dot: 10 });
    const pts = items.map(it => [it.x + out[it.key].dx, it.y + out[it.key].dy]);
    for (let i = 0; i < 3; i++) {
      for (let j = i + 1; j < 3; j++) {
        expect(Math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1])).toBeGreaterThanOrEqual(15 - 1e-6);
      }
    }
  });

  it('still fans out the dots while the names are hidden', () => {
    const out = layoutStationMarkers([{ key: 'a', x: 0, y: 0 }, { key: 'b', x: 0, y: 0 }], { dot: 8.4 });
    expect(Math.hypot(out.a.dx - out.b.dx, out.a.dy - out.b.dy)).toBeCloseTo(12.6, 9);
    expect(out.a.side).toBe('right');
    expect(out.b.side).toBe('right');
  });
});

describe('layoutStationMarkers name sides', () => {
  it('tries right, left, above and below before the corners', () => {
    expect(NAME_SIDES.slice(0, 4)).toEqual(['right', 'left', 'above', 'below']);
    // A dot right of the station blocks the right side only.
    const out = layoutStationMarkers([
      { key: 'a', x: 0, y: 0, w: 60, h: 10 }, { key: 'b', x: 30, y: 0 },
    ], { dot: 10 });
    expect(out.a.side).toBe('left');
  });

  it('keeps two names on the same row from overlapping', () => {
    // b's name on the right would run into c's dot, and on the left into a's
    // name; above is free.
    const out = layoutStationMarkers([
      { key: 'a', x: 0, y: 0, w: 40, h: 10 },
      { key: 'b', x: 60, y: 0, w: 40, h: 10 },
      { key: 'c', x: 90, y: 0 },
    ], { dot: 10 });
    expect(out.a.side).toBe('right');
    expect(out.b.side).toBe('above');
    expect(out.b.overlap).toBe(0);
  });

  it('moves an earlier name out of the way when a later one has no other side', () => {
    // a is placed first and takes the right; b's only side clear of dots is
    // its left, which a's name covers, so a moves above.
    const out = layoutStationMarkers([
      { key: 'a', x: 0, y: 0, w: 40, h: 10 },
      { key: 'b', x: 70, y: 0, w: 30, h: 10 },
      { key: 'c', x: 90, y: 0 }, { key: 'd', x: 70, y: -20 }, { key: 'e', x: 70, y: 20 },
      { key: 'f', x: 95, y: -15 }, { key: 'g', x: 95, y: 15 }, { key: 'h', x: 45, y: -15 }, { key: 'i', x: 45, y: 15 },
    ], { dot: 10 });
    expect(out.b.side).toBe('left');
    expect(out.b.overlap).toBe(0);
    expect(out.a.side).not.toBe('right');
    expect(out.a.overlap).toBe(0);
  });

  it('picks the least-overlapping position when none is free', () => {
    // Eight obstacle dots on a 25 px ring (19 px apart, so they stay put)
    // touch every position of a's name.
    const ring = Array.from({ length: 8 }, (_, k) => ({
      key: `o${k}`, x: 25 * Math.cos((k * Math.PI) / 4), y: 25 * Math.sin((k * Math.PI) / 4),
    }));
    const a = { key: 'a', x: 0, y: 0, w: 50, h: 10 };
    const out = layoutStationMarkers([a, ...ring], { dot: 10 });
    const cost = side => {
      const b = nameBox(side, 0, 0, 5, 50, 10, 3);
      return ring.reduce((c, o) => c + boxOverlap(b, { x0: o.x - 5, y0: o.y - 5, x1: o.x + 5, y1: o.y + 5 }), 0);
    };
    const costs = NAME_SIDES.map(cost);
    expect(Math.min(...costs)).toBeGreaterThan(0);
    expect(out.a.side).toBe(NAME_SIDES[costs.indexOf(Math.min(...costs))]);
    expect(out.a.overlap).toBeCloseTo(Math.min(...costs), 9);
    for (const o of ring) expect(out[o.key]).toMatchObject({ dx: 0, dy: 0 });
  });
});
