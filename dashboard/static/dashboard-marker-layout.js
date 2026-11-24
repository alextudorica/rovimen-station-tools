// Station markers on the Network overview map: where each dot and its name go
// when stations sit a few pixels apart (gmnro11 (Tărâța), gmnro16 (Bacău) and
// gmnro26 (Buhoci) within 30 px at zoom 7; gmnro17 and gmnro19 (Câmpenița) on
// the same coordinates). Pure screen-space geometry, no Leaflet and no DOM:
// dashboard-overview.js measures the map and applies the result.

/** Name positions around the dot, in order of preference. The corners are
 *  only needed zoomed out (below zoom ~6.25 on the 2026-11 fleet). */
export const NAME_SIDES = ['right', 'left', 'above', 'below', 'above-right', 'below-right', 'above-left', 'below-left'];

const EPS = 1e-6;

const byKey = (a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0);

/** Area shared by two boxes {x0, y0, x1, y1}. */
export function boxOverlap(a, b) {
  const w = Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0);
  const h = Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0);
  return w > 0 && h > 0 ? w * h : 0;
}

/** The name's box on one side of a dot centred at (cx, cy) with radius r.
 *  A corner box touches the dot's 45-degree point plus the gap. */
export function nameBox(side, cx, cy, r, w, h, gap) {
  const k = (r + gap) * Math.SQRT1_2;
  switch (side) {
    case 'above-right': return { x0: cx + k, y0: cy - k - h, x1: cx + k + w, y1: cy - k };
    case 'below-right': return { x0: cx + k, y0: cy + k, x1: cx + k + w, y1: cy + k + h };
    case 'above-left': return { x0: cx - k - w, y0: cy - k - h, x1: cx - k, y1: cy - k };
    case 'below-left': return { x0: cx - k - w, y0: cy + k, x1: cx - k, y1: cy + k + h };
    case 'left': return { x0: cx - r - gap - w, y0: cy - h / 2, x1: cx - r - gap, y1: cy + h / 2 };
    case 'above': return { x0: cx - w / 2, y0: cy - r - gap - h, x1: cx + w / 2, y1: cy - r - gap };
    case 'below': return { x0: cx - w / 2, y0: cy + r + gap, x1: cx + w / 2, y1: cy + r + gap + h };
    default: return { x0: cx + r + gap, y0: cy - h / 2, x1: cx + r + gap + w, y1: cy + h / 2 };
  }
}

// Spread one cluster evenly on the smallest circle that keeps neighbours
// `sep` apart, centred on the members' true centroid. Members keep their
// angular order around the centroid, rotated as little as possible, so a
// pair is pushed apart along the line joining it. Identical points have no
// direction: they go clockwise from the top in key order.
function fanCluster(members, pts, sep, out) {
  const n = members.length;
  const cx = members.reduce((s, i) => s + pts[i].x, 0) / n;
  const cy = members.reduce((s, i) => s + pts[i].y, 0) / n;
  const radius = sep / (2 * Math.sin(Math.PI / n));
  const step = (2 * Math.PI) / n;
  const spread = members.some(i => Math.hypot(pts[i].x - cx, pts[i].y - cy) > EPS);
  let order = members;
  let start = -Math.PI / 2;
  if (spread) {
    const ang = new Map(members.map(i => [i, Math.atan2(pts[i].y - cy, pts[i].x - cx)]));
    order = [...members].sort((a, b) => ang.get(a) - ang.get(b) || a - b);
    let sx = 0;
    let sy = 0;
    order.forEach((i, k) => { sx += Math.cos(ang.get(i) - k * step); sy += Math.sin(ang.get(i) - k * step); });
    start = Math.hypot(sx, sy) > EPS ? Math.atan2(sy, sx) : ang.get(order[0]);
  }
  order.forEach((i, k) => {
    const a = start + k * step;
    out[i] = { x: cx + radius * Math.cos(a), y: cy + radius * Math.sin(a) };
  });
}

/**
 * Lay out the station markers.
 *
 * @param {Array<{key: string, x: number, y: number, w?: number, h?: number}>} items
 *   each station's true position in container pixels and its name's on-screen
 *   size (0 while the names are hidden)
 * @param {{dot: number, gap?: number, minSep?: number, keepSep?: number, pad?: number}} opts
 *   dot: on-screen dot diameter; gap: px between a dot's edge and its name;
 *   minSep: stations whose true positions are closer than this (in dot
 *   diameters) fan out to this spacing; keepSep: a fanned dot that lands
 *   closer than this to another dot pulls it into its cluster (lower than
 *   minSep, so a pair fanned towards a neighbour 1.5 dots away leaves the
 *   neighbour in place); pad: extra margin around each name box (none by
 *   default: a side name is taller than the dot plus both gaps, so any
 *   margin would block a neighbour's name above or below it)
 * @returns {Object<string, {dx: number, dy: number, side: string, overlap: number}>}
 *   per key: the dot's offset from its true position, the name's side, and
 *   the area (px²) the name still overlaps (0 unless no side was free)
 */
export function layoutStationMarkers(items, { dot, gap = 3, minSep = 1.5, keepSep = 1.25, pad = 0 } = {}) {
  const st = [...items].sort(byKey);
  const n = st.length;
  const sep = minSep * dot;
  const pts = st.map(s => ({ x: s.x, y: s.y }));

  // 1. Fan out dots closer than `sep`. Clusters grow (union-find) until no
  //    two dots of different clusters are closer than `keepSep` after fanning.
  const parent = st.map((_, i) => i);
  const find = i => { while (parent[i] !== i) i = parent[i] = parent[parent[i]]; return i; };
  let pos = pts.map(p => ({ ...p }));
  for (let pass = 0; pass < n; pass++) {
    const limit = pass === 0 ? sep : keepSep * dot;
    let merged = false;
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        if (Math.hypot(pos[i].x - pos[j].x, pos[i].y - pos[j].y) >= limit - EPS) continue;
        const a = find(i);
        const b = find(j);
        if (a !== b) { parent[Math.max(a, b)] = Math.min(a, b); merged = true; }
      }
    }
    if (!merged) break;
    const groups = new Map();
    for (let i = 0; i < n; i++) {
      const r = find(i);
      if (!groups.has(r)) groups.set(r, []);
      groups.get(r).push(i);
    }
    pos = pts.map(p => ({ ...p }));
    for (const members of groups.values()) if (members.length > 1) fanCluster(members, pts, sep, pos);
  }

  // 2. Pick each name's side, stations in key order: the first side in
  //    NAME_SIDES whose box touches no dot and no name already placed and
  //    does not sit nearer another station's dot than its own. When
  //    every side is taken, names in the way are moved to other free sides
  //    (a few levels deep); failing that, the least-overlapping side wins.
  const r = dot / 2;
  const dots = pos.map(p => ({ x0: p.x - r, y0: p.y - r, x1: p.x + r, y1: p.y + r }));
  const sides = new Array(n).fill('right');
  const boxes = new Array(n).fill(null);
  const named = i => (st[i].w || 0) > 0 && (st[i].h || 0) > 0;
  const boxFor = (i, side) => {
    const b = nameBox(side, pos[i].x, pos[i].y, r, st[i].w, st[i].h, gap);
    return { x0: b.x0 - pad, y0: b.y0 - pad, x1: b.x1 + pad, y1: b.y1 + pad };
  };
  const dotCost = (i, box) => dots.reduce((c, d, j) => (j === i ? c : c + boxOverlap(box, d)), 0);
  const nameCost = (i, box) => boxes.reduce((c, b, j) => (j === i || !b ? c : c + boxOverlap(box, b)), 0);
  const blockers = (i, box) => boxes.flatMap((b, j) => (j !== i && b && boxOverlap(box, b) > EPS ? [j] : []));
  const set = (i, side, box) => { sides[i] = side; boxes[i] = box; };
  // A name reads as another station's when that station's dot is nearer to
  // it than its own (a corner name right beside the neighbour's dot): such
  // a position is tried only after the unambiguous ones.
  const reach = (box, p) => Math.hypot(Math.max(box.x0 - p.x, 0, p.x - box.x1), Math.max(box.y0 - p.y, 0, p.y - box.y1));
  const ambiguous = (i, box) => {
    const own = reach(box, pos[i]);
    return pos.some((p, j) => j !== i && reach(box, p) < own - EPS);
  };

  // Give name i a side clear of dots and names, moving the names in the way
  // (not those in `locked`) when depth allows. Leaves everything as it was
  // when it fails.
  const tryPlace = (i, depth, locked) => {
    const open = NAME_SIDES.map(side => ({ side, box: boxFor(i, side) }))
      .filter(c => dotCost(i, c.box) <= EPS)
      .sort((a, b) => ambiguous(i, a.box) - ambiguous(i, b.box));
    for (const c of open) {
      if (nameCost(i, c.box) <= EPS) { set(i, c.side, c.box); return true; }
    }
    if (depth <= 0) return false;
    for (const c of open) {
      const ins = blockers(i, c.box);
      if (ins.some(j => locked.has(j))) continue;
      const before = [sides.slice(), boxes.slice()];
      const lock = new Set(locked).add(i);
      set(i, c.side, c.box);
      let ok = true;
      for (const j of ins) {
        if (!boxes[j] || boxOverlap(c.box, boxes[j]) <= EPS) continue;
        boxes[j] = null;
        if (!tryPlace(j, depth - 1, lock)) { ok = false; break; }
      }
      if (ok) return true;
      before[0].forEach((sd, k) => { sides[k] = sd; });
      before[1].forEach((b, k) => { boxes[k] = b; });
    }
    return false;
  };
  const leastBad = i => {
    let pick = null;
    for (const side of NAME_SIDES) {
      const box = boxFor(i, side);
      const c = dotCost(i, box) + nameCost(i, box);
      if (!pick || c < pick.c - EPS) pick = { side, box, c };
    }
    set(i, pick.side, pick.box);
  };
  const REPAIR_DEPTH = 3;
  for (let i = 0; i < n; i++) {
    if (named(i) && !tryPlace(i, REPAIR_DEPTH, new Set())) leastBad(i);
  }
  // A name placed early saw none of the later ones: retry any that overlap.
  for (let i = 0; i < n; i++) {
    if (!named(i) || dotCost(i, boxes[i]) + nameCost(i, boxes[i]) <= EPS) continue;
    const keep = [sides[i], boxes[i]];
    boxes[i] = null;
    if (!tryPlace(i, REPAIR_DEPTH, new Set())) set(i, ...keep);
  }

  const out = {};
  st.forEach((s, i) => {
    out[s.key] = {
      dx: pos[i].x - s.x,
      dy: pos[i].y - s.y,
      side: sides[i],
      overlap: named(i) ? dotCost(i, boxes[i]) + nameCost(i, boxes[i]) : 0,
    };
  });
  return out;
}
