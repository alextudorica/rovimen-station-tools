/*
 * dashboard-overview.js
 * ─────────────────────
 * The Network overview "/network" (overview.html, ov_page 'network'): the
 * fleet map with camera fields of view, coverage layers, the station
 * selection band, Bortle and GMN orbit layers, the live snapshot view, the
 * station status table and "Cameras at a point". The home page "/" runs
 * dashboard-home.js instead and never loads this module; the top bar both
 * pages share is drawn by renderOverviewSummary and loadOverviewNetStats in
 * dashboard-common.js.
 */
import { state, escHtml, fetchJson, openAppPanel, appPanelClose, _modalOpen, _modalClose, createPollBackoff, serviceIsUp, SEGMENT_ALARM_TEXT, fetchAuthStatus, setPressed, renderOverviewSummary, loadOverviewNetStats } from './dashboard-common.js';
import { hideTarget } from './dashboard-hidden.js';
import { VideoModal } from './dashboard-video-modal.js';
import {
  PICK_ALT_MIN_KM, PICK_ALT_MAX_KM, altitudeSamples, camerasSeeingPoint, groupByStation,
  stationLabel, nightForUtc, gridWindowFor, gridUrlFor, utcDateTimeParts,
} from './dashboard-map-point.js';
import { layoutStationMarkers } from './dashboard-marker-layout.js';

const _BASE_LAYER_BTNS = '#map-layer-btns .map-seg .fov-btn';
const _reducedMotion = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
// "HH:MM:SS" in UTC; the caller appends the " UTC" label.
const _utcTimeFmt = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'UTC', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
});
/* global _gmnOpenClipModal, _gmnCloseClipModal */
// The shared VideoModal shows operator-only controls off state.AUTH_USER,
// which the page's one session request writes; anonymous visitors keep
// {user:null} → playback-only.
fetchAuthStatus();

let STATION_COORDS = {};
let PLATEPAR_DATA  = {};
let FOV_ALT_KM     = 25;
const R_EARTH_KM   = 6371;
let footprintLayer = null;
let mapData        = null;
let stationColors  = {};
let camPolygons    = {};  // { 'gmnro03_RO000H': polygon }
let camLabels      = {};  // { 'gmnro03_RO000H': L.marker label }
let fovVisible     = false;
// state.activeStation is declared in dashboard-common.js (shared across pages);
// assigning here without `let` mutates that binding.
state.activeStation = null;
// Map of host key → Leaflet marker, populated while drawing the overview map.
// Used to highlight selected markers and to look up info for the band.
const stationMarkers = {};
// Cached station info objects (from /api/stations) keyed by host, so the
// selection band can rebuild without re-reading the marker loop closure.
const stationInfoByKey = {};
// FOV-visibility state captured the first time a station is selected, so
// clearStationSelection can restore exactly what the user saw before.
let fovStateBeforeSelect = false;

// ── vector helpers ─────────────────────────────────────────────────────────────
function v3cross(a,b){return[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]]}
function v3norm(a){return Math.sqrt(a[0]*a[0]+a[1]*a[1]+a[2]*a[2])}
function v3unit(a){const n=v3norm(a);return[a[0]/n,a[1]/n,a[2]/n]}
function v3add(a,b){return[a[0]+b[0],a[1]+b[1],a[2]+b[2]]}
function v3scale(a,s){return[a[0]*s,a[1]*s,a[2]*s]}

// Returns { polygon, center } — center is the true focal projection point (nx=0, ny=0)
// Equatorial projection: uses RA_d, dec_d, pos_angle_ref from platepar when available.
// This matches RMS's internal model and correctly handles wide-angle FOVs.
// Fallback (no equatorial params): uses alt-az great-circle rotation with rotation_from_horiz.
function cameraFootprint(lat, lon, azDeg, altDeg, rotDeg, fovH, fovV, color, H,
                          xPolyRaw, Fscale, xRes, yRes, equalAspect, asymCorr,
                          raDeg, decDeg, posAngleRef, distortionType, yPolyRaw,
                          maskContour) {
  const D2R=Math.PI/180, MIN_ELEV=5*D2R;
  const N=20;
  const azR=azDeg*D2R, altR=altDeg*D2R;

  const useDist = !!(xPolyRaw?.length >= 5 && Fscale && xRes && yRes);
  const isPoly = useDist && distortionType && distortionType.startsWith('poly3+radial');

  let x0n=0, y0n=0, xy=0, a1=0, a2=0, k1=0, k2=0, k3=0;
  if (useDist && !isPoly) {
    let i=0, p=xPolyRaw;
    x0n=p[i++]||0; y0n=p[i++]||0;
    if (!equalAspect) xy=p[i++]||0;
    if (asymCorr) { a1=p[i++]||0; a2=(p[i++]||0)*2*Math.PI; }
    k1=p[i++]||0; k2=p[i++]||0; k3=p[i++]||0;
  }

  function applyDist(px, py) {
    if (!useDist) return [px*(fovH/2), py*(fovV/2)];
    const xImg=px*(xRes/2), yImg=py*(yRes/2);

    if (isPoly) {
      const xp=xPolyRaw, yp=yPolyRaw||xPolyRaw;
      const x0p=xp[0]||0, y0p=yp[0]||0;
      const r=Math.sqrt((xImg-x0p)**2+(yImg-y0p)**2);
      let dx=x0p
        +(xp[1]||0)*xImg+(xp[2]||0)*yImg
        +(xp[3]||0)*xImg**2+(xp[4]||0)*xImg*yImg+(xp[5]||0)*yImg**2
        +(xp[6]||0)*xImg**3+(xp[7]||0)*xImg**2*yImg+(xp[8]||0)*xImg*yImg**2+(xp[9]||0)*yImg**3
        +(xp[10]||0)*xImg*r+(xp[11]||0)*yImg*r;
      let dy=y0p
        +(yp[1]||0)*xImg+(yp[2]||0)*yImg
        +(yp[3]||0)*xImg**2+(yp[4]||0)*xImg*yImg+(yp[5]||0)*yImg**2
        +(yp[6]||0)*xImg**3+(yp[7]||0)*xImg**2*yImg+(yp[8]||0)*xImg*yImg**2+(yp[9]||0)*yImg**3
        +(yp[10]||0)*yImg*r+(yp[11]||0)*xImg*r;
      if(distortionType.endsWith('+radial3')||distortionType.endsWith('+radial5')){
        dx+=(xp[12]||0)*xImg*r**3; dy+=(yp[12]||0)*yImg*r**3;
      }
      if(distortionType.endsWith('+radial5')){
        dx+=(xp[13]||0)*xImg*r**5; dy+=(yp[13]||0)*yImg*r**5;
      }
      return [(xImg+dx)/Fscale, (yImg+dy)/Fscale];
    }

    const x0=x0n*(xRes/2), y0=y0n*(yRes/2);
    const dxc=xImg-x0, dyc=(1+xy)*(yImg-y0);
    let r=Math.sqrt(dxc*dxc+dyc*dyc);
    r+=a1*dyc*Math.cos(a2)-a1*dxc*Math.sin(a2);
    r/=(xRes/2);
    const rc=r+k1*r**3+k2*r**5+k3*r**7;
    const rs=r>1e-9?(rc/r-1):0;
    const xc=xImg+((xImg-x0)*rs-x0);
    const yc=yImg+((yImg-y0)*rs*(1+xy)-y0*(1+xy)+yImg*xy);
    return [xc/Fscale, yc/Fscale];
  }

  // Ray direction (ENU) → geographic lat/lon at altitude H km.
  // Spherical geometry (law of sines for range, haversine for destination).
  function rayToGeo(dE, dN, dU) {
    const elev=Math.max(Math.atan2(dU,Math.sqrt(dE*dE+dN*dN)),MIN_ELEV);
    const bearing=Math.atan2(dE,dN);
    const rm=R_EARTH_KM+H;
    const beta=elev+Math.asin(R_EARTH_KM*Math.cos(elev)/rm);
    const ca=Math.PI/2-beta;
    if(ca<=0) return [lat,lon];
    const lat0=lat*D2R, lon0=lon*D2R;
    const sinCA=Math.sin(ca), cosCA=Math.cos(ca);
    const sinLat0=Math.sin(lat0), cosLat0=Math.cos(lat0);
    const lat2=Math.asin(sinLat0*cosCA+cosLat0*sinCA*Math.cos(bearing));
    const lon2=lon0+Math.atan2(Math.sin(bearing)*sinCA*cosLat0,cosCA-sinLat0*Math.sin(lat2));
    return [lat2/D2R, lon2/D2R];
  }

  let projectDeg;

  const useEquatorial = !!(raDeg!=null && decDeg!=null && posAngleRef!=null);
  if (useEquatorial) {
    // Equatorial projection matching RMS CyFunctions xyToRaDecPP
    const latR=lat*D2R, decR=decDeg*D2R, raR=raDeg*D2R, posR=posAngleRef*D2R;
    // Derive LST from stored physical az/alt + stored RA/Dec
    const sinHA=-Math.sin(azR)*Math.cos(altR)/Math.cos(decR);
    const cosHA=(Math.sin(altR)-Math.sin(latR)*Math.sin(decR))/(Math.cos(latR)*Math.cos(decR));
    const HA_c=Math.atan2(sinHA,cosHA);
    const LST=(raR+HA_c+2*Math.PI)%(2*Math.PI);

    projectDeg=function(xi_deg, eta_deg) {
      // Position angle in equatorial tangent plane (theta=0 → equatorial North)
      const theta=Math.PI/2-posR+Math.atan2(eta_deg,xi_deg);
      const radius=Math.sqrt(xi_deg*xi_deg+eta_deg*eta_deg)*D2R;
      if(radius<1e-9) return rayToGeo(Math.cos(altR)*Math.sin(azR),Math.cos(altR)*Math.cos(azR),Math.sin(altR));
      // Spherical cap → Dec
      const sinDec=Math.sin(decR)*Math.cos(radius)+Math.cos(decR)*Math.sin(radius)*Math.cos(theta);
      const decPix=Math.asin(Math.max(-1,Math.min(1,sinDec)));
      // → RA
      const sinT=Math.sin(theta)*Math.sin(radius)/Math.cos(decPix);
      const cosT=(Math.cos(radius)-sinDec*Math.sin(decR))/(Math.cos(decPix)*Math.cos(decR));
      const raPix=(raR-Math.atan2(sinT,cosT)+2*Math.PI)%(2*Math.PI);
      // RA/Dec → HA → alt/az → ENU ray
      const HA=(LST-raPix+2*Math.PI)%(2*Math.PI);
      const sinAlt=Math.sin(latR)*sinDec+Math.cos(latR)*Math.cos(decPix)*Math.cos(HA);
      const altPix=Math.asin(Math.max(-1,Math.min(1,sinAlt)));
      const altPixSafe=Math.max(altPix,MIN_ELEV);
      const azNum=-Math.cos(decPix)*Math.sin(HA)/Math.cos(altPixSafe);
      const azDen=(sinDec-Math.sin(altPixSafe)*Math.sin(latR))/(Math.cos(altPixSafe)*Math.cos(latR));
      const azPix=(Math.atan2(azNum,azDen)+2*Math.PI)%(2*Math.PI);
      return rayToGeo(Math.cos(altPixSafe)*Math.sin(azPix),Math.cos(altPixSafe)*Math.cos(azPix),Math.sin(altPixSafe));
    };
  } else {
    // Fallback: alt-az great-circle rotation (less accurate for wide-angle but works without RA/Dec)
    const rotR=-(rotDeg||0)*D2R;
    const oz=[Math.cos(altR)*Math.sin(azR),Math.cos(altR)*Math.cos(azR),Math.sin(altR)];
    let cr0=v3cross(oz,[0,0,1]);
    cr0=v3norm(cr0)<1e-6?[1,0,0]:v3unit(cr0);
    const cu0=v3unit(v3cross(cr0,oz));
    const cosR=Math.cos(rotR),sinR=Math.sin(rotR);
    const cr=v3add(v3scale(cr0,cosR),v3scale(cu0,-sinR));
    const cu=v3add(v3scale(cr0,sinR),v3scale(cu0, cosR));
    projectDeg=function(xi_deg, eta_deg) {
      const xi_r=xi_deg*D2R, eta_r=eta_deg*D2R;
      const radius=Math.sqrt(xi_r*xi_r+eta_r*eta_r);
      // Optical-axis short-circuit: project oz itself (matches the
      // equatorial branch above). Returning [lat,lon] here used to
      // stack all camera labels on the station icon when applyDist
      // returned exactly (0,0) — which happens for any placeholder
      // platepar without distortion polys.
      if(radius<1e-9) return rayToGeo(oz[0],oz[1],oz[2]);
      const sinRad=Math.sin(radius),cosRad=Math.cos(radius);
      const xi_n=xi_r/radius, eta_n=eta_r/radius;
      const d=[cosRad*oz[0]+sinRad*(cr[0]*xi_n+cu[0]*eta_n),
               cosRad*oz[1]+sinRad*(cr[1]*xi_n+cu[1]*eta_n),
               cosRad*oz[2]+sinRad*(cr[2]*xi_n+cu[2]*eta_n)];
      return rayToGeo(d[0],d[1],d[2]);
    };
  }

  function project(nx, ny) {
    const [xi,eta]=applyDist(nx,ny);
    return projectDeg(xi,eta);
  }

  const pts=[];
  // When the station supplies a mask contour (boundary of the unmasked sky,
  // in normalised sensor coords), project that loop so the rendered FOV reflects
  // the actual observable area (trees / buildings / horizon clipped out).
  // Otherwise walk the full sensor edges (original behaviour).
  const useMask = Array.isArray(maskContour) && maskContour.length >= 3;
  if (useMask) {
    for (const p of maskContour) {
      if (!Array.isArray(p) || p.length < 2) continue;
      pts.push(project(p[0], p[1]));
    }
  }
  if (pts.length < 3) {
    pts.length = 0;
    for(let k=0;k<=N;k++) pts.push(project((k/N)*2-1,-1));
    for(let k=1;k<=N;k++) pts.push(project(1,(k/N)*2-1));
    for(let k=1;k<=N;k++) pts.push(project(1-(k/N)*2,1));
    for(let k=1;k<=N;k++) pts.push(project(-1,1-(k/N)*2));
  }
  const center = project(0, 0);
  if (color == null) return { pts, center };
  const polygon = L.polygon(pts,{color,fillColor:color,fillOpacity:0.18,weight:1.8,opacity:0.7});
  return { polygon, center };
}

// Ground distance (km) at which a meteor at height H sits 10 deg above the
// horizon: the map draws no footprint beyond it.
function _footprintCapKm(H) {
  const ce=Math.cos(10*Math.PI/180);
  const beta=10*Math.PI/180+Math.asin(R_EARTH_KM*ce/(R_EARTH_KM+H));
  return R_EARTH_KM*(Math.PI/2-beta);
}

// Densify a footprint ring (24 points per edge) and pull every point beyond
// `capKm` from the station back onto that radius. Pure: [lat, lon] pairs in
// and out, so the map and the point picker share it.
export function _capFootprintPts(pts, lat, lon, capKm) {
  // The edge walk ends on its first point; drop the closing duplicate as
  // L.polygon does, so the ring has no zero-length edge.
  const last=pts[pts.length-1];
  if(pts.length>1 && last[0]===pts[0][0] && last[1]===pts[0][1]) pts=pts.slice(0,-1);
  const cosLat=Math.cos(lat*Math.PI/180);
  const SEGS=24;
  const out=[];
  for(let j=0;j<pts.length;j++){
    const a=pts[j], b=pts[(j+1)%pts.length];
    for(let t=0;t<SEGS;t++){
      const f=t/SEGS;
      const pLat=a[0]+(b[0]-a[0])*f, pLon=a[1]+(b[1]-a[1])*f;
      const dLat=pLat-lat, dLon=pLon-lon;
      const dist=Math.sqrt((dLat*111)**2+(dLon*111*cosLat)**2);
      if(dist<=capKm) { out.push([pLat,pLon]); continue; }
      const s=capKm/dist;
      out.push([lat+dLat*s, lon+dLon*s]);
    }
  }
  return out;
}

// One camera's footprint at height H as capped [lat, lon] pairs plus the
// optical-axis point, from its platepar (falling back to the registry az/alt).
// Null when the camera has no pointing. The map draws exactly this ring.
export function _cameraFootprintPts(lat, lon, cam, pp, H) {
  const az=pp?.az_centre??cam.az, alt=pp?.alt_centre??cam.alt;
  if(az==null||alt==null) return null;
  const result=cameraFootprint(lat,lon,az,alt,pp?.rotation_from_horiz??0,pp?.fov_h??90,pp?.fov_v??50,null,H,
    pp?.x_poly_fwd??pp?.x_poly, pp?.F_scale, pp?.X_res, pp?.Y_res,
    pp?.equal_aspect??true, pp?.asymmetry_corr??true,
    pp?.RA_d, pp?.dec_d, pp?.pos_angle_ref,
    pp?.distortion_type, pp?.y_poly_fwd??pp?.y_poly,
    pp?.mask_contour);
  if(!result) return null;
  return { pts: _capFootprintPts(result.pts, lat, lon, _footprintCapKm(H)), center: result.center };
}

function stationFovIds(key) {
  return Object.keys(camPolygons).filter(id=>id.startsWith(key+'_'));
}

// Pure helper: union of every FOV id in `allIds` belonging to any host in
// `keys` (ids are `<host>_<cam>`). Exported for unit tests.
export function _unionFovIds(keys, allIds) {
  const wanted = keys instanceof Set ? keys : new Set(keys);
  return allIds.filter(id => {
    const host = id.slice(0, id.lastIndexOf('_'));
    return wanted.has(host);
  });
}

function _addFovPair(id) {
  if(camPolygons[id]) footprintLayer.addLayer(camPolygons[id]);
  if(camLabels[id])   footprintLayer.addLayer(camLabels[id]);
}
function _removeFovPair(id) {
  if(camPolygons[id]) footprintLayer.removeLayer(camPolygons[id]);
  if(camLabels[id])   footprintLayer.removeLayer(camLabels[id]);
}

function restoreAllFov() {
  Object.keys(camPolygons).forEach(id => _addFovPair(id));
}

function hideAllFov() {
  footprintLayer.clearLayers();
}

function toggleAllFov(btn) {
  // The global toggle owns FOV outright, so drop any active selection first
  // (silent: this function manages the FOV/band state itself afterwards).
  if (state.selectedStations.size) clearStationSelection({ silent: true });
  fovVisible = !fovVisible;
  state.activeStation = null;
  if(fovVisible) restoreAllFov();
  else hideAllFov();
  btn.textContent = fovVisible ? 'Hide FOV' : 'Show FOV';
  btn.classList.toggle('active', fovVisible);
}

window.togglePanelFov = function(btn) {
  const host = btn?.dataset?.fovHost || state.activeStation;
  if(!host) return;
  const ids = stationFovIds(host);
  const anyVisible = ids.some(id=>footprintLayer.hasLayer(camPolygons[id]));
  if(anyVisible) {
    ids.forEach(id => _removeFovPair(id));
    btn.textContent = 'Show FOV';
  } else {
    ids.forEach(id => _addFovPair(id));
    if(!map.hasLayer(footprintLayer)) footprintLayer.addTo(map);
    btn.textContent = 'Hide FOV';
  }
};

/* ─────────────────────────────────────────
   Multi-select stations on the overview map
   (issue #511) — toggle without zoom, union FOV
───────────────────────────────────────── */

// Pure helper: mutate a Set by toggling `key`. Returns whether the key is
// now present. Exported for unit tests.
export function _toggleInSet(set, key) {
  if (set.has(key)) { set.delete(key); return false; }
  set.add(key);
  return true;
}

function _setMarkerSelected(key, selected) {
  const marker = stationMarkers[key];
  if (!marker) return;
  const el = marker.getElement?.();
  if (!el) return;
  const dot = el.querySelector?.('.ov-marker-dot') || el;
  dot.classList.toggle('ov-marker-selected', selected);
}

// Add or remove one station's FOV pairs from the footprint layer without
// touching any other station (union semantics).
function _setStationFov(key, visible) {
  const ids = stationFovIds(key);
  if (visible) {
    ids.forEach(id => _addFovPair(id));
    if (ids.length && !map.hasLayer(footprintLayer)) footprintLayer.addTo(map);
  } else {
    ids.forEach(id => _removeFovPair(id));
  }
}

// Re-apply the union FOV for every currently-selected station. Called after
// the footprint geometry is rebuilt (e.g. altitude change).
function _applySelectionFov() {
  hideAllFov();
  for (const key of state.selectedStations) _setStationFov(key, true);
}

function toggleStationSelection(key, info) {
  if (info) stationInfoByKey[key] = info;
  // Capture the pre-selection FOV state once, before the first selection,
  // so Clear all can restore exactly what was on screen.
  if (state.selectedStations.size === 0) fovStateBeforeSelect = fovVisible;

  const nowSelected = _toggleInSet(state.selectedStations, key);
  _setMarkerSelected(key, nowSelected);
  _setStationFov(key, nowSelected);

  if (nowSelected) {
    state.activeStation = key;
  } else if (state.activeStation === key) {
    // Point activeStation at any remaining selection (legacy controls
    // expect a non-null host while a selection exists).
    const remaining = [...state.selectedStations];
    state.activeStation = remaining.length ? remaining[remaining.length - 1] : null;
  }

  if (state.selectedStations.size === 0) {
    clearStationSelection();
    return;
  }
  _setGlobalFovToggleDisabled(true);
  renderSelectionBand();
}

// Clears the whole selection: empties the Set, drops marker highlights,
// restores the pre-selection FOV state, hides/rebuilds the band, and resets
// activeStation. `silent` skips the FOV restore (used when the caller is
// already managing FOV, e.g. the global toggle).
function clearStationSelection({ silent = false } = {}) {
  for (const key of state.selectedStations) _setMarkerSelected(key, false);
  state.selectedStations.clear();
  state.activeStation = null;

  if (_sbThumbTimer) { clearInterval(_sbThumbTimer); _sbThumbTimer = null; }
  if (_sbAgeTimer) { clearInterval(_sbAgeTimer); _sbAgeTimer = null; }

  const band = document.getElementById('station-band');
  if (band) band.style.display = 'none';
  const sbInner = document.getElementById('sb-inner');
  if (sbInner) sbInner.replaceChildren();
  document.getElementById('map-wrap')?.classList.remove('ov-band-open');

  if (!silent) {
    if (fovStateBeforeSelect) restoreAllFov();
    else hideAllFov();
    fovVisible = fovStateBeforeSelect;
    const toggleBtn = document.getElementById('fov-toggle');
    if (toggleBtn) {
      toggleBtn.textContent = fovVisible ? 'Hide FOV' : 'Show FOV';
      toggleBtn.classList.toggle('active', fovVisible);
    }
  }
  fovStateBeforeSelect = false;
  _setGlobalFovToggleDisabled(false);
}

function setFovAlt(km, btn) {
  FOV_ALT_KM = km;
  document.querySelectorAll('.fov-alt-group .fov-btn').forEach(b => setPressed(b, b === btn));
  if(mapData) {
    drawFootprints(mapData, km);
    if(state.selectedStations.size) _applySelectionFov();
    else if(fovVisible) restoreAllFov();
  }
  // drawFootprints calls _precomputeCovGrid which auto-refreshes active coverage layers
}

const _covLayers = {1: null, 2: null, 3: null};
const _covActive = {1: false, 2: false, 3: false};
let _covPrecomp = null;

function toggleCovLayer(n, btn) {
  _covActive[n] = !_covActive[n];
  setPressed(btn, _covActive[n]);
  if (_covActive[n]) {
    _showCovLayer(n);
  } else if (_covLayers[n] && map) {
    map.removeLayer(_covLayers[n]);
    _covLayers[n] = null;
  }
}

function _showCovLayer(n) {
  if (!_covPrecomp || !map) return;
  if (_covLayers[n] && map) map.removeLayer(_covLayers[n]);
  _covLayers[n] = L.layerGroup();
  const p = _covPrecomp;

  if (n === 1) {
    // One polygon per station with fillRule nonzero so overlapping same-station
    // cameras fill uniformly instead of creating holes
    for (const rings of Object.values(p.stationRings)) {
      L.polygon(rings, {
        color: 'none', fillColor: '#a5d6ff', fillOpacity: 0.08,
        interactive: false, fillRule: 'nonzero',
      }).addTo(_covLayers[1]);
    }
  } else {
    // n=2: >=2 stations, n=3: >=3 stations
    const minN = n === 2 ? 2 : 3;
    const color = n === 2 ? '#58a6ff' : '#1f6feb';
    const opacity = n === 2 ? 0.10 : 0.15;
    const step = p.step, halfLat = step / 2;
    for (const cell of p.gridCells) {
      if (cell.n >= minN) {
        const halfLon = step / 2 / Math.cos(cell.lat * Math.PI / 180);
        L.rectangle(
          [[cell.lat - halfLat, cell.lon - halfLon], [cell.lat + halfLat, cell.lon + halfLon]],
          { color: 'none', fillColor: color, fillOpacity: opacity, interactive: false }
        ).addTo(_covLayers[n]);
      }
    }
  }
  _covLayers[n].addTo(map);
}

function _precomputeCovGrid() {
  _covPrecomp = null;
  const stationRings = {};
  for (const [id, poly] of Object.entries(camPolygons)) {
    const host = id.split('_')[0];
    if (!stationRings[host]) stationRings[host] = [];
    const latlngs = poly.getLatLngs();
    const ring = Array.isArray(latlngs[0]) ? latlngs[0] : latlngs;
    stationRings[host].push(ring.map(ll => [ll.lat, ll.lng]));
  }
  // 1-station data is ready immediately (just polygon refs)
  _covPrecomp = { stationRings, step: 0.08, gridCells: [] };
  if (_covActive[1]) _showCovLayer(1);

  // Grid for 2+/3+ computed async in chunks to avoid blocking UI
  const stationEntries = Object.entries(stationRings);
  if (stationEntries.length < 2) return;

  let minLat=90,maxLat=-90,minLon=180,maxLon=-180;
  for (const [, rings] of stationEntries) {
    for (const ring of rings) {
      for (const [lat,lon] of ring) {
        if (lat < minLat) minLat = lat;
        if (lat > maxLat) maxLat = lat;
        if (lon < minLon) minLon = lon;
        if (lon > maxLon) maxLon = lon;
      }
    }
  }

  // Build flat bounding boxes for quick rejection
  const stBounds = stationEntries.map(([, rings]) => {
    let sMinLat=90,sMaxLat=-90,sMinLon=180,sMaxLon=-180;
    for (const ring of rings) {
      for (const [lat,lon] of ring) {
        if (lat < sMinLat) sMinLat = lat;
        if (lat > sMaxLat) sMaxLat = lat;
        if (lon < sMinLon) sMinLon = lon;
        if (lon > sMaxLon) sMaxLon = lon;
      }
    }
    return {minLat: sMinLat, maxLat: sMaxLat, minLon: sMinLon, maxLon: sMaxLon, rings};
  });

  const step = 0.08, cells = [];
  const latSteps = [];
  for (let lat = minLat; lat <= maxLat; lat += step) latSteps.push(lat);
  let idx = 0;
  const CHUNK = 50;

  function processChunk() {
    const end = Math.min(idx + CHUNK, latSteps.length);
    for (; idx < end; idx++) {
      const lat = latSteps[idx];
      for (let lon = minLon; lon <= maxLon; lon += step) {
        let count = 0;
        for (const sb of stBounds) {
          if (lat < sb.minLat || lat > sb.maxLat || lon < sb.minLon || lon > sb.maxLon) continue;
          for (const ring of sb.rings) {
            if (_ptInRing(lat, lon, ring)) { count++; break; }
          }
        }
        if (count >= 2) cells.push({lat, lon, n: count});
      }
    }
    if (idx < latSteps.length) {
      setTimeout(processChunk, 0);
    } else {
      _covPrecomp.gridCells = cells;
      for (const k of [2,3]) { if (_covActive[k]) _showCovLayer(k); }
    }
  }
  setTimeout(processChunk, 0);
}

function _ptInRing(lat, lon, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const yi = ring[i][0], xi = ring[i][1];
    const yj = ring[j][0], xj = ring[j][1];
    if (((yi > lat) !== (yj > lat)) && (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi))
      inside = !inside;
  }
  return inside;
}

function _setGlobalFovToggleDisabled(disabled) {
  const btn = document.getElementById('fov-toggle');
  if(!btn) return;
  btn.disabled = disabled;
  btn.style.opacity = disabled ? '0.35' : '';
  btn.style.cursor  = disabled ? 'default' : '';
}

// Legacy entry point retained for the map background-click handler: closing
// the panel now means clearing the whole selection.
function closePanel() {
  if (!state.selectedStations.size) return;
  clearStationSelection();
}

// Per-station FOV toggle button (carries its host via data-fov-host so each
// row's button operates independently in the multi-selection band).
function _makeFovButton(key) {
  if (!stationFovIds(key).length) return null;
  const btn = document.createElement('button');
  btn.className = 'sb-btn';
  btn.dataset.fovHost = key;
  const anyVisible = stationFovIds(key).some(id => footprintLayer.hasLayer(camPolygons[id]));
  btn.textContent = anyVisible ? 'Hide FOV' : 'Show FOV';
  btn.onclick = function() { window.togglePanelFov(this); };
  return btn;
}

// GMN orbit counts for the station band (the hover card is off on touch).
function _orbitsText(key) {
  if (!_stationOrbits?.ready) return 'GMN orbits loading…';
  const o = _stationOrbits.stations?.[key] || { orbits_30d: 0, orbits_all: 0 };
  return `GMN orbits: ${o.orbits_30d.toLocaleString('en-US')} (30 d) · ${o.orbits_all.toLocaleString('en-US')} all time`;
}
function _makeOrbitsSpan(key) {
  const el = document.createElement('span');
  el.className = 'sb-orbits';
  el.dataset.orbitKey = key;
  el.textContent = _orbitsText(key);
  return el;
}
function _refreshBandOrbits() {
  for (const el of document.querySelectorAll('.sb-orbits')) el.textContent = _orbitsText(el.dataset.orbitKey);
}

// Per-station remove (✕): deselect just this host, leaving the rest.
function _makeRemoveButton(key, info) {
  const btn = document.createElement('button');
  btn.className = 'sb-close';
  btn.textContent = '✕';
  btn.title = `Remove ${key} from selection`;
  btn.onclick = function() { toggleStationSelection(key, info); };
  return btn;
}

// Rich single-station card (thumbnails) — used when exactly one station is
// selected. No flyTo and no FOV mutation: selection logic owns the FOV.
function _renderSingleStationCard(key, info, sbInner) {
  const lat = info.lat ?? STATION_COORDS[key]?.lat;
  const lon = info.lon ?? STATION_COORDS[key]?.lon;
  const color = stationColors[key] || 'var(--blue)';

  const coordStr = (lat && lon)
    ? `${Math.abs(lat).toFixed(2)}°${lat>=0?'N':'S'} ${Math.abs(lon).toFixed(2)}°${lon>=0?'E':'W'}`
    : '';

  const bortleInfo = _bortleCache[key];

  const infoRow = document.createElement('div');
  infoRow.style.cssText = 'display:flex;align-items:center;gap:20px;flex-wrap:wrap;width:100%';

  const nameDiv = document.createElement('div');
  const nameLink = document.createElement('a');
  nameLink.className = 'sb-name';
  nameLink.href = `/station/${encodeURIComponent(key)}`;
  nameLink.style.cssText = 'color:inherit;text-decoration:none;border-bottom:1px dotted currentColor';
  nameLink.title = 'Go to station page';
  nameLink.textContent = key;
  nameDiv.appendChild(nameLink);
  if (info.label) {
    const labelSpan = document.createElement('span');
    labelSpan.className = 'sb-label';
    labelSpan.textContent = ` — ${info.label}`;
    nameDiv.appendChild(labelSpan);
  }
  infoRow.appendChild(nameDiv);

  if (coordStr) {
    const coordSpan = document.createElement('span');
    coordSpan.className = 'sb-coords';
    coordSpan.textContent = coordStr;
    infoRow.appendChild(coordSpan);
  }
  if (bortleInfo) {
    const bSpan = document.createElement('span');
    bSpan.title = `Bortle ${bortleInfo.cls} — ${bortleInfo.label}`;
    bSpan.style.cssText = `font-size:11px;padding:2px 8px;border-radius:3px;border:1px solid;color:${bortleColor(bortleInfo.cls)};border-color:${bortleColor(bortleInfo.cls)}55;font-family:'SF Mono',monospace;white-space:nowrap`;
    bSpan.textContent = `Bortle ${bortleInfo.cls}`;
    infoRow.appendChild(bSpan);
  }

  const camsDiv = document.createElement('div');
  camsDiv.className = 'sb-cams';
  for (const c of (info.cameras || [])) {
    const a = document.createElement('a');
    a.className = 'sb-cam-badge';
    a.href = `/station/${encodeURIComponent(key)}/videodb?cam=${encodeURIComponent(c.code)}`;
    a.style.cssText = `color:${color};border-color:${color}33;text-decoration:none`;
    a.title = `Open ${c.code} in the Video DB`;
    a.textContent = c.code;
    camsDiv.appendChild(a);
  }
  infoRow.appendChild(camsDiv);
  infoRow.appendChild(_makeOrbitsSpan(key));

  const actionsDiv = document.createElement('div');
  actionsDiv.className = 'sb-actions';
  const fovBtn = _makeFovButton(key);
  if (fovBtn) actionsDiv.appendChild(fovBtn);
  const goLink = document.createElement('a');
  goLink.href = `/station/${encodeURIComponent(key)}`;
  goLink.className = 'sb-btn go';
  goLink.textContent = 'Go to station →';
  actionsDiv.appendChild(goLink);
  actionsDiv.appendChild(_makeRemoveButton(key, info));
  infoRow.appendChild(actionsDiv);

  sbInner.appendChild(infoRow);

  // FF maxpixel thumbnails — shown when any RMS service on the station is active
  const rmsActive = _isRmsActive(info.services);
  const activeCams = rmsActive ? (info.cameras || []) : [];
  if (activeCams.length) {
    const thumbRow = document.createElement('div');
    thumbRow.className = 'sb-thumbs';
    sbInner.appendChild(thumbRow);
    _sbThumbRefresh(key, activeCams, thumbRow);
  }
}

// Compact one-line row per station — used when ≥2 stations are selected.
function _renderCompactRow(key, info, sbInner) {
  const color = stationColors[key] || 'var(--blue)';
  const row = document.createElement('div');
  row.className = 'sb-row';
  row.style.cssText = 'display:flex;align-items:center;gap:12px;flex-wrap:wrap;width:100%';

  const nameLink = document.createElement('a');
  nameLink.className = 'sb-name';
  nameLink.href = `/station/${encodeURIComponent(key)}`;
  nameLink.style.cssText = 'color:inherit;text-decoration:none;border-bottom:1px dotted currentColor';
  nameLink.title = info.label ? `${info.label} — go to station page` : 'Go to station page';
  nameLink.textContent = key;
  row.appendChild(nameLink);
  row.appendChild(_makeOrbitsSpan(key));

  const camsDiv = document.createElement('div');
  camsDiv.className = 'sb-cams';
  for (const c of (info.cameras || [])) {
    const a = document.createElement('a');
    a.className = 'sb-cam-badge';
    a.href = `/station/${encodeURIComponent(key)}/videodb?cam=${encodeURIComponent(c.code)}`;
    a.style.cssText = `color:${color};border-color:${color}33;text-decoration:none`;
    a.title = `Open ${c.code} in the Video DB`;
    a.textContent = c.code;
    camsDiv.appendChild(a);
  }
  row.appendChild(camsDiv);

  const actionsDiv = document.createElement('div');
  actionsDiv.className = 'sb-actions';
  const fovBtn = _makeFovButton(key);
  if (fovBtn) actionsDiv.appendChild(fovBtn);
  actionsDiv.appendChild(_makeRemoveButton(key, info));
  row.appendChild(actionsDiv);

  sbInner.appendChild(row);
}

// Rebuild the selection band from state.selectedStations.
function renderSelectionBand() {
  const sbInner = document.getElementById('sb-inner');
  if (!sbInner) return;
  // Tear down single-card thumbnail timers before re-rendering.
  if (_sbThumbTimer) { clearInterval(_sbThumbTimer); _sbThumbTimer = null; }
  if (_sbAgeTimer) { clearInterval(_sbAgeTimer); _sbAgeTimer = null; }
  sbInner.replaceChildren();

  const keys = [...state.selectedStations];
  if (!keys.length) {
    closePanel();
    return;
  }

  if (keys.length === 1) {
    const key = keys[0];
    _renderSingleStationCard(key, stationInfoByKey[key] || {}, sbInner);
  } else {
    const list = document.createElement('div');
    list.className = 'sb-list';
    list.style.cssText = 'display:flex;flex-direction:column;gap:8px;width:100%';
    for (const key of keys) _renderCompactRow(key, stationInfoByKey[key] || {}, list);
    sbInner.appendChild(list);

    const footer = document.createElement('div');
    footer.className = 'sb-band-footer';
    footer.style.cssText = 'display:flex;align-items:center;gap:10px;width:100%;margin-top:6px';
    const count = document.createElement('span');
    count.className = 'sb-coords';
    count.textContent = `${keys.length} stations selected`;
    footer.appendChild(count);
    sbInner.appendChild(footer);
  }

  // Single "Clear all" control, always present while a selection exists.
  const clearBtn = document.createElement('button');
  clearBtn.className = 'sb-btn';
  clearBtn.id = 'sb-clear-all';
  clearBtn.textContent = 'Clear all';
  clearBtn.style.marginLeft = 'auto';
  clearBtn.onclick = function() { clearStationSelection(); };
  if (keys.length > 1) {
    sbInner.querySelector('.sb-band-footer')?.appendChild(clearBtn);
  } else {
    const actions = sbInner.querySelector('.sb-actions');
    if (actions) actions.insertBefore(clearBtn, actions.firstChild);
    else sbInner.appendChild(clearBtn);
  }

  document.getElementById('station-band').style.display = 'block';
  document.getElementById('map-wrap')?.classList.add('ov-band-open');
}

function _isRmsActive(services) {
  if (!services) return false;
  for (const [key, val] of Object.entries(services)) {
    if (key.startsWith('rms-')) {
      if (serviceIsUp(val)) return true;
    }
  }
  return false;
}

let _sbThumbTimer = null;
let _sbAgeTimer = null;
let _sbThumbLoad = null;   // the band poller's tick, for the visibility catch-up

// Both 15 s thumbnail pollers (station band, Live View) skip their ticks
// while the tab is hidden: a map left open in a background tab polled
// /api/latest_ff_maxpixel around the clock (134,675 requests, 26 Aug to
// 26 Sep). Returning to the tab refreshes at once instead of waiting a tick.
document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  if (_sbThumbTimer && _sbThumbLoad) _sbThumbLoad();
  if (_liveVisible) _liveRefresh();
});

// Shared across the station band and Live View: both poll the same
// /api/latest_ff_maxpixel/<host>/<cam> endpoint every 15 s, so a station that
// is down should stop being probed by BOTH of them. Keyed per camera endpoint
// (a station's cameras go quiet together, but one dead camera on a healthy
// station must not silence its siblings). See createPollBackoff().
const _ffThumbBackoff = createPollBackoff();

// True when the overview's own /api/overview snapshot says the station is
// down. Polling it at all is wasted: the endpoint answers 503 only AFTER
// trying to reach the station, so every skipped call is an upstream timeout
// avoided. Unknown/absent station -> treat as pollable (fail open).
function _stationLooksOffline(hostKey) {
  const info = mapData && mapData[hostKey];
  return !!info && info.online === false;
}

// One gate for both pollers: skip while backed off or while the station is
// known offline, and count the offline skip as a failure so a station that is
// down for the whole night settles onto the 15 min ceiling instead of being
// re-probed the moment it flickers back into the map data.
function _ffThumbShouldSkip(hostKey, camCode) {
  const key = hostKey + '/' + camCode;
  if (_ffThumbBackoff.blocked(key)) return true;
  if (_stationLooksOffline(hostKey)) {
    _ffThumbBackoff.fail(key);
    return true;
  }
  return false;
}

function _sbThumbRefresh(hostKey, cams, container) {
  if (_sbThumbTimer) clearInterval(_sbThumbTimer);
  if (_sbAgeTimer) clearInterval(_sbAgeTimer);
  const load = () => {
    if (!document.contains(container)) {
      clearInterval(_sbThumbTimer); _sbThumbTimer = null;
      clearInterval(_sbAgeTimer); _sbAgeTimer = null;
      _sbThumbLoad = null;
      return;
    }
    if (document.hidden) return;
    for (const c of cams) {
      if (_ffThumbShouldSkip(hostKey, c.code)) continue;
      const backoffKey = hostKey + '/' + c.code;
      const url = `/api/latest_ff_maxpixel/${encodeURIComponent(hostKey)}/${encodeURIComponent(c.code)}`;
      fetch(url).then(resp => {
        if (!resp.ok) {
          _ffThumbBackoff.fail(backoffKey);
          const old = container.querySelector(`[data-cam="${c.code}"]`);
          if (old) {
            const _s = old.querySelector('img')?.src;
            if (_s && _s.startsWith('blob:')) URL.revokeObjectURL(_s);
            old.remove();
          }
          return;
        }
        _ffThumbBackoff.succeed(backoffKey);
        const ffTs = resp.headers.get('X-FF-Timestamp');
        return resp.blob().then(blob => {
          let card = container.querySelector(`[data-cam="${c.code}"]`);
          if (!card) {
            card = document.createElement('div');
            card.className = 'sb-thumb';
            card.dataset.cam = c.code;
            const img = document.createElement('img');
            img.alt = c.code;
            if (c.rotate) img.style.transform = 'rotate(180deg)';
            const lbl = document.createElement('div');
            lbl.className = 'sb-thumb-label';
            card.append(img, lbl);
            container.appendChild(card);
          }
          if (ffTs) card.dataset.ts = ffTs;
          const img = card.querySelector('img');
          const oldSrc = img.src;
          img.src = URL.createObjectURL(blob);
          if (oldSrc.startsWith('blob:')) URL.revokeObjectURL(oldSrc);
          _sbUpdateLabel(card);
        });
      }).catch(() => { _ffThumbBackoff.fail(backoffKey); });
    }
  };
  _sbThumbLoad = load;
  load();
  _sbThumbTimer = setInterval(load, 15000);
  _sbAgeTimer = setInterval(_sbUpdateAllLabels, 5000);
}

function _sbUpdateLabel(card) {
  const lbl = card.querySelector('.sb-thumb-label');
  if (!lbl) return;
  const code = card.dataset.cam || '';
  const ts = card.dataset.ts;
  lbl.textContent = ts ? code + '  ' + _ffAge(ts) : code;
}

function _sbUpdateAllLabels() {
  for (const card of document.querySelectorAll('.sb-thumb[data-ts]')) _sbUpdateLabel(card);
}

function _ffAge(isoStr) {
  const sec = Math.max(0, Math.floor((Date.now() - new Date(isoStr).getTime()) / 1000));
  if (sec < 60) return 't-' + sec + 's';
  const min = Math.floor(sec / 60);
  return 't-' + min + 'min';
}

// ── Live View grid ──────────────────────────────────────────────────────────

let _liveTimer = null;
let _liveAgeTimer = null;
let _liveVisible = false;

function _setLiveBtnLabel(label) {
  for (const id of ['ov-live-bar-btn', 'ov-stacks-live-btn']) {
    const b = document.getElementById(id);
    if (b) b.textContent = label;
  }
}

function toggleLiveView() {
  const el = document.getElementById('ov-live');
  if (!el) return;
  if (_liveVisible) {
    _liveVisible = false;
    el.style.display = 'none';
    if (_liveTimer) { clearInterval(_liveTimer); _liveTimer = null; }
    if (_liveAgeTimer) { clearInterval(_liveAgeTimer); _liveAgeTimer = null; }
    // Release blob-backed thumbnails on hide; _liveRefresh rebuilds the grid
    // from scratch on reopen, so stranding them here would leak until unload.
    const liveGrid = document.getElementById('ov-live-grid');
    if (liveGrid) {
      for (const img of liveGrid.querySelectorAll('img')) {
        if (img.src && img.src.startsWith('blob:')) URL.revokeObjectURL(img.src);
      }
      liveGrid.innerHTML = '';
    }
    _setLiveBtnLabel('View live snapshot of all cameras in the fleet');
  } else {
    _liveVisible = true;
    el.style.display = '';
    _liveRefresh();
    _liveTimer = setInterval(_liveRefresh, 15000);
    _liveAgeTimer = setInterval(_liveUpdateAges, 5000);
    el.scrollIntoView({behavior: _reducedMotion() ? 'auto' : 'smooth', block: 'nearest'});
    _setLiveBtnLabel('Hide the live snapshot');
  }
}

// Card width is a CSS variable, so cards the 15 s refresh adds pick it up too.
function ovLiveUpdateZoom(val) {
  const px = parseInt(val, 10) || 160;
  document.documentElement.style.setProperty('--ov-live-w', px + 'px');
}

// TODO(#340): _liveRefresh fires bare fetch() calls without an AbortController.
// If the tab is hidden or the panel toggled off mid-flight, in-progress requests
// complete and update the DOM after teardown. Consider wiring an AbortController
// that is aborted in toggleLiveView's hide branch.
function _liveRefresh() {
  if (!mapData || document.hidden) return;
  const grid = document.getElementById('ov-live-grid');
  let count = 0;
  const stationColors = {};
  const palette = ['#58a6ff','#3fb950','#d29922','#bc8cff','#f85149','#79c0ff','#e3b341','#ff7b72','#7ee787','#a5d6ff'];
  let ci = 0;
  for (const [hostKey, info] of Object.entries(mapData)) {
    if (!_isRmsActive(info.services)) continue;
    stationColors[hostKey] = palette[ci++ % palette.length];
    let groupEl = document.getElementById('live-group-' + hostKey);
    if (!groupEl) {
      groupEl = document.createElement('div');
      groupEl.className = 'ov-live-group';
      groupEl.id = 'live-group-' + hostKey;
      groupEl.style.borderColor = stationColors[hostKey];
      const label = document.createElement('div');
      label.className = 'ov-live-group-label';
      label.style.color = stationColors[hostKey];
      label.textContent = (info.label || hostKey);
      groupEl.appendChild(label);
      const inner = document.createElement('div');
      inner.className = 'ov-live-group-cards';
      groupEl.appendChild(inner);
      grid.appendChild(groupEl);
    }
    const inner = groupEl.querySelector('.ov-live-group-cards');
    for (const c of (info.cameras || [])) {
      // Counted before the backoff gate so the "N cameras capturing" label
      // reads exactly as it did before: the grid still shows the station, we
      // just stop re-asking a station that isn't answering.
      count++;
      if (_ffThumbShouldSkip(hostKey, c.code)) continue;
      const backoffKey = hostKey + '/' + c.code;
      const cardId = 'live-' + hostKey + '-' + c.code;
      const url = '/api/latest_ff_maxpixel/' + encodeURIComponent(hostKey) + '/' + encodeURIComponent(c.code);
      fetch(url).then(resp => {
        if (!resp.ok) { _ffThumbBackoff.fail(backoffKey); return; }
        _ffThumbBackoff.succeed(backoffKey);
        const ffTs = resp.headers.get('X-FF-Timestamp');
        return resp.blob().then(blob => {
          if (!_liveVisible) return;
          let card = document.getElementById(cardId);
          if (!card) {
            card = document.createElement('button');
            card.type = 'button';
            card.className = 'ov-live-card';
            card.id = cardId;
            card.dataset.host = hostKey;
            card.dataset.cam = c.code;
            card.setAttribute('aria-label', c.code + ': open the latest frame and clip');
            card.onclick = () => _liveOpenVideo(hostKey, c.code);
            const img = document.createElement('img');
            img.alt = '';
            if (c.rotate) img.style.transform = 'rotate(180deg)';
            const meta = document.createElement('span');
            meta.className = 'ov-live-meta';
            const codeSpan = document.createElement('span');
            codeSpan.textContent = c.code;
            const ageSpan = document.createElement('span');
            ageSpan.className = 'ov-live-age';
            meta.append(codeSpan, ageSpan);
            card.append(img, meta);
            inner.appendChild(card);
          }
          if (ffTs) card.dataset.ts = ffTs;
          const img = card.querySelector('img');
          const oldSrc = img.src;
          img.src = URL.createObjectURL(blob);
          if (oldSrc.startsWith('blob:')) URL.revokeObjectURL(oldSrc);
          _liveUpdateCardAge(card);
        });
      }).catch(() => { _ffThumbBackoff.fail(backoffKey); });
    }
  }
  document.getElementById('ov-live-count').textContent =
    count > 0 ? captureHeadline(captureSummary(mapData)) : 'no active cameras';
  for (const card of [...grid.querySelectorAll('.ov-live-card')]) {
    const h = card.dataset.host;
    if (!mapData[h] || !_isRmsActive(mapData[h].services)) {
      const _s = card.querySelector('img')?.src;
      if (_s && _s.startsWith('blob:')) URL.revokeObjectURL(_s);
      _ffThumbBackoff.forget(h + '/' + card.dataset.cam);
      card.remove();
      const group = document.getElementById('live-group-' + h);
      if (group && !group.querySelector('.ov-live-card')) group.remove();
    }
  }
}

function _liveUpdateCardAge(card) {
  const ts = card.dataset.ts;
  const age = card.querySelector('.ov-live-age');
  if (!age || !ts) return;
  age.textContent = _ffAge(ts);
  const dt = new Date(ts);
  const secsAgo = (Date.now() - dt.getTime()) / 1000;
  if (secsAgo > 600) age.style.color = 'var(--red)';
  else if (secsAgo > 300) age.style.color = '#e89b3c';
  else age.style.color = '';
}

function _liveUpdateAges() {
  for (const card of document.querySelectorAll('.ov-live-card[data-ts]')) {
    _liveUpdateCardAge(card);
  }
}

// Live-tile click: show the tile's FF maxpixel full size at once, then swap to
// the latest clip when the station has it ready. The newest chunk is raw at
// night and the station burns the overlay in before sending a byte (7-9 s on
// VAAPI hosts, up to ~1 min on a Pi), so /api/latest_chunk starts that render
// and we poll it for the same chunk. Every wait is bounded and every failure
// is shown; the still stays up whatever happens.
export const LIVE_CLIP_WAIT_MS = 120000;   // render + poll budget
export const LIVE_CLIP_POLL_MS = 2500;
export const LIVE_CLIP_LOAD_MS = 45000;    // overlaid clip -> first frame
let _liveClip = null;  // {token, timers[]} for the modal's current attempt

function _liveClipEls() {
  return {
    modal: document.getElementById('ov-live-modal'),
    video: document.getElementById('ov-live-video'),
    still: document.getElementById('ov-live-still'),
    label: document.getElementById('ov-live-video-label'),
    status: document.getElementById('ov-live-status'),
  };
}

function _liveClipStop() {
  if (_liveClip) for (const t of _liveClip.timers) clearTimeout(t);
  _liveClip = null;
}

function _liveClipLater(clip, fn, ms) {
  clip.timers.push(setTimeout(() => { if (_liveClip === clip) fn(); }, ms));
}

function _liveSetStatus(text, isError, retry) {
  const { status } = _liveClipEls();
  if (!status) return;
  status.classList.toggle('error', !!isError);
  status.textContent = text;
  if (retry) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = 'Retry';
    b.onclick = retry;
    status.appendChild(b);
  }
}

function _liveResetVideo(video) {
  if (!video) return;
  video.pause();
  video.oncanplay = video.onerror = null;
  video.removeAttribute('src');
  video.load();
  video.style.display = 'none';
}

export function _liveOpenVideo(hostKey, camCode) {
  _liveClipStop();
  const clip = { timers: [] };
  _liveClip = clip;
  const { modal, video, still, label } = _liveClipEls();
  if (!modal || !video) return;
  const retry = () => _liveOpenVideo(hostKey, camCode);

  // 1) The still: the tile's own frame, already in memory, full size.
  const card = document.getElementById('live-' + hostKey + '-' + camCode);
  const tileImg = card && card.querySelector('img');
  _liveResetVideo(video);
  if (still) {
    const ffUrl = '/api/latest_ff_maxpixel/' + encodeURIComponent(hostKey) + '/' +
                  encodeURIComponent(camCode);
    // The tile's blob URL is revoked when the tile refreshes (every 15 s);
    // fall back to the endpoint if that happens before the still decodes.
    still.onerror = () => { still.onerror = null; still.src = ffUrl; };
    still.src = (tileImg && tileImg.src) || ffUrl;
    still.style.transform = (tileImg && tileImg.style.transform) || '';
    still.style.display = '';
  }
  if (label) {
    const age = card && card.dataset.ts ? '  ' + _ffAge(card.dataset.ts) : '';
    label.textContent = camCode + '  latest FF maxpixel' + age;
  }
  _liveSetStatus('Loading the latest clip…', false);
  _modalOpen(modal);

  const started = Date.now();
  const fail = (msg) => { _liveSetStatus(msg, true, retry); _liveClip = null; };
  const play = (data) => {
    _liveSetStatus('Loading clip ' + (data.time || '') + ' UTC…', false);
    video.oncanplay = () => {
      if (_liveClip !== clip) return;
      if (still) still.style.display = 'none';
      video.style.display = '';
      if (label) label.textContent = camCode + '  ' + (data.time || '') + ' UTC';
      _liveSetStatus('', false);
      _liveClipStop();
    };
    video.onerror = () => {
      if (_liveClip !== clip) return;
      _liveResetVideo(video);
      fail('The clip for ' + (data.time || '') + ' UTC failed to load; showing the latest FF maxpixel.');
    };
    video.src = data.video_url;
    _liveClipLater(clip, () => {
      _liveResetVideo(video);
      fail('The clip did not start within ' + Math.round(LIVE_CLIP_LOAD_MS / 1000) +
           ' s; showing the latest FF maxpixel.');
    }, LIVE_CLIP_LOAD_MS);
  };
  const base = '/api/latest_chunk/' + encodeURIComponent(hostKey) + '/' + encodeURIComponent(camCode);
  const poll = (query) => {
    fetchJson(base + query).then(data => {
      if (_liveClip !== clip) return;
      if (!data || !data.video_url) { fail('No clip available for ' + camCode + '.'); return; }
      // rendering:false on a not-yet-overlaid chunk = the dashboard could not
      // start a background render (per-worker cap); loading the clip directly
      // makes the station render it, bounded by LIVE_CLIP_LOAD_MS.
      if (data.overlaid || data.rendering === false) { play(data); return; }
      if (Date.now() - started >= LIVE_CLIP_WAIT_MS) {
        fail('The station has not finished preparing ' + (data.time || '') + ' UTC after ' +
             Math.round(LIVE_CLIP_WAIT_MS / 1000) + ' s; showing the latest FF maxpixel.');
        return;
      }
      const secs = Math.round((Date.now() - started) / 1000);
      _liveSetStatus('Station is burning in the overlay for ' + (data.time || '') + ' UTC, ' +
                     secs + ' s (up to a minute on a Raspberry Pi)…', false);
      const next = '?date=' + encodeURIComponent(data.date) +
                   '&filename=' + encodeURIComponent(data.filename);
      _liveClipLater(clip, () => poll(next), LIVE_CLIP_POLL_MS);
    }).catch(err => {
      if (_liveClip !== clip) return;
      fail(err && err.status === 404 ? 'No recent clip on ' + camCode + '.'
                                     : 'Could not reach the station for ' + camCode + '.');
    });
  };
  poll('');
}

function closeLiveModal() {
  _liveClipStop();
  const { modal, video, still, status } = _liveClipEls();
  _liveResetVideo(video);
  if (still) { still.removeAttribute('src'); still.style.display = 'none'; }
  if (status) status.textContent = '';
  _modalClose(modal);
}

const COLORS=['#58a6ff','#3fb950','#d29922','#bc8cff','#f85149','#79c0ff','#e3b341'];

function drawFootprints(data, H) {
  if(!footprintLayer) return;
  footprintLayer.clearLayers();
  camPolygons = {};
  camLabels   = {};
  _loadStationOrbits();
  let i=0;
  for(const [key,info] of Object.entries(data)){
    const lat=info.lat??STATION_COORDS[key]?.lat;
    const lon=info.lon??STATION_COORDS[key]?.lon;
    if(!lat||!lon||info.show_on_map===false){i++;continue}
    const color=COLORS[i%COLORS.length];
    stationColors[key]=color;
    const pps=PLATEPAR_DATA[key]||{};
    for(const cam of (info.cameras||[])){
      const fp=_cameraFootprintPts(lat,lon,cam,pps[cam.code],H);
      if(!fp) continue;
      const id=`${key}_${cam.code}`;
      camPolygons[id]=L.polygon(fp.pts,{color,fillColor:color,fillOpacity:0.18,weight:1.8,opacity:0.7});
      camLabels[id]=L.marker(fp.center,{
        icon:L.divIcon({
          className:'',
          html:`<span class="cam-fov-label">${cam.code}</span>`,
          iconSize:null,iconAnchor:null,
        }),
        interactive:false,
      });
    }
    i++;
  }
  _precomputeCovGrid();
}

// ── Light pollution / Bortle ──────────────────────────────────────────────────

const LP_Z = 6;          // max native zoom in tile URLs (Leaflet maxNativeZoom:8 with zoomOffset:-2 → URL z=6)
const LP_TILE_W = 1024;  // tile size in pixels

// Reference palette for Lorenz 2024 atlas (RGB → Bortle class).
// Colors derived from the atlas colorbar; refined empirically.
// cls=Bortle class, pr/pg/pb=reference pixel color (empirically sampled from Lorenz 2024 atlas)
const BORTLE_REF = [
  { cls:1, pr:0,   pg:0,   pb:0,   label:'Pristine dark sky' },          // ocean/wilderness
  { cls:2, pr:34,  pg:34,  pb:34,  label:'Excellent dark sky' },          // remote desert (dark gray)
  { cls:2, pr:20,  pg:47,  pb:114, label:'Excellent dark sky' },          // Sahara (dark navy)
  { cls:3, pr:15,  pg:87,  pb:20,  label:'Rural sky' },                   // rural France (dark green)
  { cls:3, pr:33,  pg:84,  pb:216, label:'Rural sky' },                   // rural France (dark blue)
  { cls:4, pr:110, pg:100, pb:30,  label:'Rural/suburban transition' },   // small town (olive)
  { cls:5, pr:31,  pg:161, pb:42,  label:'Suburban sky' },                // suburban fringe (bright green)
  { cls:5, pr:184, pg:166, pb:37,  label:'Suburban sky' },                // village near a city (yellow-green)
  { cls:6, pr:191, pg:100, pb:30,  label:'Bright suburban sky' },         // town (orange)
  { cls:7, pr:251, pg:153, pb:138, label:'Suburban/urban transition' },   // capital suburb (salmon)
  { cls:8, pr:160, pg:160, pb:160, label:'City sky' },                    // capital centre (gray)
  { cls:9, pr:242, pg:242, pb:242, label:'Inner-city sky' },              // London/Paris (near-white)
];

function _rgbToBortle(r, g, b) {
  let best = BORTLE_REF[0], bestDist = Infinity;
  for (const ref of BORTLE_REF) {
    const d = (r-ref.pr)**2 + (g-ref.pg)**2 + (b-ref.pb)**2;
    if (d < bestDist) { bestDist = d; best = ref; }
  }
  return { cls: best.cls, label: best.label };
}

function _latLonToTilePixel(lat, lon) {
  const n = Math.pow(2, LP_Z);
  const latR = lat * Math.PI / 180;
  const sec = 1 / Math.cos(latR);
  const xTile = Math.floor((lon + 180) / 360 * n);
  const yTile = Math.floor((1 - Math.log(Math.tan(latR) + sec) / Math.PI) / 2 * n);
  const xFrac = (lon + 180) / 360 * n - xTile;
  const yFrac = (1 - Math.log(Math.tan(latR) + sec) / Math.PI) / 2 * n - yTile;
  return { xTile, yTile, px: Math.floor(xFrac * LP_TILE_W), py: Math.floor(yFrac * LP_TILE_W) };
}

const _bortleCache = {};   // host_key → {bortle, label, r, g, b}
const LP_LS_KEY = 'ov_bortle_v4';

function _saveBortleCache() {
  try { localStorage.setItem(LP_LS_KEY, JSON.stringify(_bortleCache)); } catch(e) {}
}

(function _loadBortleCache() {
  try {
    const saved = JSON.parse(localStorage.getItem(LP_LS_KEY) || '{}');
    Object.assign(_bortleCache, saved);
  } catch(e) {}
})();

async function _sampleBortle(lat, lon) {
  const { xTile, yTile, px, py } = _latLonToTilePixel(lat, lon);
  const url = `https://djlorenz.github.io/astronomy/image_tiles/tiles2024/tile_${LP_Z}_${xTile}_${yTile}.png`;
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img.onload = () => {
      try {
        const cv = document.createElement('canvas');
        cv.width = img.width; cv.height = img.height;
        cv.getContext('2d').drawImage(img, 0, 0);
        const [r, g, b] = cv.getContext('2d').getImageData(px, py, 1, 1).data;
        const { cls, label } = _rgbToBortle(r, g, b);
        resolve({ cls, label, r, g, b });
      } catch(e) { reject(e); }
    };
    img.onerror = reject;
    img.src = url;
  });
}

async function loadBortleData(stationData) {
  for (const [key, info] of Object.entries(stationData)) {
    if (_bortleCache[key]) continue;   // already cached
    const lat = info.lat ?? STATION_COORDS[key]?.lat;
    const lon = info.lon ?? STATION_COORDS[key]?.lon;
    if (!lat || !lon) continue;
    try {
      _bortleCache[key] = await _sampleBortle(lat, lon);
    } catch(e) {
      console.warn(`Bortle sample failed for ${key}:`, e);
    }
  }
  _saveBortleCache();
}

function bortleColor(cls) {
  const colors = ['','#a8d8ea','#7ec8e3','#4db8c8','#52c452','#c8d448','#e8c820','#e88020','#e05050','#ff3030'];
  return colors[cls] || '#888';
}

// ── Map layers ────────────────────────────────────────────────────────────────

const MAP_LAYERS = {
  topo: {
    url: 'https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
    opts: { attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, <a href="http://viewfinderpanoramas.org">SRTM</a> | <a href="https://opentopomap.org">OpenTopoMap</a>', maxZoom:17 },
  },
  short: {
    // Esri Dark Gray Canvas — keyless. CARTO deprecated keyless basemap tiles,
    // which now come back as an "API KEY REQUIRED" watermark. Esri REST tiles
    // use {z}/{y}/{x} order, no {s} subdomains and no {r} retina suffix.
    url: 'https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}',
    // The base is land + country outlines only; city labels, roads and finer
    // borders come from this transparent Reference overlay (composited on top).
    ref: 'https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}',
    opts: { attribution:'Tiles &copy; <a href="https://www.esri.com/">Esri</a> &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors', maxZoom:19, maxNativeZoom:16 },
  },
  std: {
    url: 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    opts: { attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors', maxZoom:19 },
  },
  sat: {
    // Esri World Imagery — keyless, global satellite/aerial mosaic.
    url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    opts: { attribution:'Tiles &copy; <a href="https://www.esri.com/">Esri</a> &mdash; Source: Esri, Maxar, Earthstar Geographics', maxZoom:19 },
  },
  light: {
    // Esri Light Gray Canvas — keyless replacement for CARTO light_all (now key-gated).
    url: 'https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}',
    ref: 'https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}',
    opts: { attribution:'Tiles &copy; <a href="https://www.esri.com/">Esri</a> &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors', maxZoom:19, maxNativeZoom:16 },
  },
};

let _tileLayer  = null;
let _refLayer   = null;   // Esri Reference overlay (labels/roads/borders) for the Gray Canvas bases
let _lpLayer    = null;
let _gmnLayer   = null;   // L.layerGroup holding track polylines + dot markers
let _gmnActive  = false;

// The Esri Gray Canvas bases carry only land + country outlines; a basemap
// ``ref`` URL supplies the transparent labels/roads/borders overlay. Composite
// it above the base tile but still below the marker / overlay panes.
function _applyRefLayer(key) {
  if (_refLayer) { map.removeLayer(_refLayer); _refLayer = null; }
  const cfg = MAP_LAYERS[key];
  if (cfg && cfg.ref) {
    _refLayer = L.tileLayer(cfg.ref, cfg.opts).addTo(map);
    _refLayer.bringToFront();
  }
}

const LP_TILE_URL = 'https://djlorenz.github.io/astronomy/image_tiles/tiles2024/tile_{z}_{x}_{y}.png';
const LP_OPTS = {
  tileSize: 1024, zoomOffset: -2, maxNativeZoom: 8, maxZoom: 19,
  opacity: 0.25, pane: 'lpPane',
  attribution: '<a href="https://djlorenz.github.io/astronomy/lp2020/" target="_blank">Light Pollution Atlas</a>',
};

function toggleLpLayer(btn) {
  if (_lpLayer) {
    map.removeLayer(_lpLayer);
    _lpLayer = null;
    setPressed(btn, false);
    localStorage.setItem('ov_lp_layer', '0');
  } else {
    _lpLayer = L.tileLayer(LP_TILE_URL, LP_OPTS).addTo(map);
    setPressed(btn, true);
    localStorage.setItem('ov_lp_layer', '1');
  }
}

function setMapBrightness(val, save) {
  const tilePaneEl = document.querySelector('.leaflet-tile-pane');
  if (tilePaneEl) tilePaneEl.style.filter = `brightness(${val/100})`;
  if (save) localStorage.setItem('ov_map_brightness', val);
}

function _setLayerMenuOpen(open) {
  document.getElementById('map-layer-btns').classList.toggle('open', open);
  document.getElementById('map-layer-toggle-btn')?.setAttribute('aria-expanded', open ? 'true' : 'false');
}
function toggleLayerMenu() {
  _setLayerMenuOpen(!document.getElementById('map-layer-btns').classList.contains('open'));
}
function switchMapLayer(key, btn) {
  _setLayerMenuOpen(false);
  // Base layers only: the Light Pollution overlay keeps its own state.
  document.querySelectorAll(_BASE_LAYER_BTNS).forEach(b => setPressed(b, b === btn));
  if(_tileLayer) map.removeLayer(_tileLayer);
  const {url, opts} = MAP_LAYERS[key];
  _tileLayer = L.tileLayer(url, opts).addTo(map);
  _tileLayer.bringToBack();
  _applyRefLayer(key);
  localStorage.setItem('ov_map_layer', key);
  // Dark basemaps (Carto dark, satellite imagery) skip the brightness dimming
  // — the filter exists to mute bright light-themed tiles on the dark UI.
  const isDark = key === 'short' || key === 'sat';
  const tilePaneEl = document.querySelector('.leaflet-tile-pane');
  const brightnessRow = document.getElementById('brightness-row');
  if (isDark) {
    if (tilePaneEl) tilePaneEl.style.filter = 'none';
    if (brightnessRow) brightnessRow.classList.add('hidden');
  } else {
    const val = document.getElementById('map-brightness').value;
    if (tilePaneEl) tilePaneEl.style.filter = `brightness(${val/100})`;
    if (brightnessRow) brightnessRow.classList.remove('hidden');
  }
}

// ── GMN multi-station orbit layer ────────────────────────────────────────────

function _gmnDefaultDate() {
  // Last completed UTC night = today UTC - 1 day
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - 1);
  return d.toISOString().slice(0, 10);  // YYYY-MM-DD
}

/* Which night the layer opens on.

   GMN solves a night 4-15 days after it happened, so seeding the picker
   with "yesterday" reliably opened the overlay on an empty map. Prefer the
   newest night upstream has actually published (from /api/gmn/latest_date)
   and keep yesterday only as the fallback for when the backend can't name
   one — a cold cache, or the call failing. */
export function _gmnSeedDate(latest, fallback) {
  return /^\d{4}-\d{2}-\d{2}$/.test(latest || '') ? latest : fallback;
}

/* What to say when a night comes back with no orbits. Two very different
   situations look identical in the payload's empty `events` list:
   "GMN hasn't published this night yet" (normal, wait for it — neutral)
   vs "published, but none of our cameras were on it" (pick another night
   — red). `not_published_yet` from the backend separates them. */
export function _gmnEmptyNotice(data) {
  const d = data || {};
  const latest = typeof d.latest_available_date === 'string' ? d.latest_available_date : null;
  // The backend flag is authoritative; the date compare is a fallback for a
  // payload cached before the flag existed.
  const notPublished = !!latest && (d.not_published_yet === true ||
                                    (typeof d.date === 'string' && d.date > latest));
  if (notPublished) {
    return {
      tone: 'info',
      text: `GMN has not published this night yet — newest available: ${latest}`,
      jumpDate: latest,
    };
  }
  return { tone: 'warn', text: 'No orbits for this night — try another date', jumpDate: null };
}

/* Paint the map banner. `tone: 'warn'` colours it red; `jumpDate` appends a
   one-click control that reopens the layer on that night. Always rewrites
   the whole banner so one branch's colour / tooltip / button can't leak
   into the next. No-ops when the banner isn't in the DOM. */
function _gmnSetBanner(banner, notice) {
  if (!banner) return;
  const n = notice || {};
  banner.textContent = n.text || '';
  banner.title = n.title || '';
  banner.style.color = n.tone === 'warn' ? 'var(--red)' : '';
  banner.classList.toggle('has-action', !!n.jumpDate);
  if (!n.jumpDate) return;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'gmn-jump-btn';
  btn.textContent = `Show ${n.jumpDate}`;
  btn.addEventListener('click', () => {
    const input = document.getElementById('gmn-date-input');
    if (input) input.value = n.jumpDate;
    _gmnFetchAndDraw(n.jumpDate);
  });
  banner.appendChild(btn);
}

/* Resolve the night to draw, seeding an untouched picker from the backend.
   A date the user already picked always wins. */
async function _gmnResolveOpenDate(dateInput) {
  if (dateInput && dateInput.value) return dateInput.value;
  let latest = null;
  try {
    const payload = await fetchJson('/api/gmn/latest_date');
    latest = payload && payload.latest_available_date;
  } catch (e) {
    // Backend down or cache still cold — yesterday still draws something.
  }
  const date = _gmnSeedDate(latest, _gmnDefaultDate());
  if (dateInput) dateInput.value = date;
  return date;
}

/* Resolve once Leaflet's map object exists.

   The layer button ships in the static template, so it can be clicked
   before initMap() has run — on a slow first paint that is easy to hit.
   Handing an undefined map to L.layerGroup().addTo() throws from inside
   Leaflet, which is both a dead layer and a page error. */
async function _gmnMapReady(timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs;
  while (!map && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  return !!map;
}

async function _gmnOpenLayer() {
  const dateInput = document.getElementById('gmn-date-input');
  const date = await _gmnResolveOpenDate(dateInput);
  if (!_gmnActive) return;   // toggled back off while the seed was in flight
  _gmnFetchAndDraw(date);
}

function _gmnFormatTime(iso) {
  // "2026-05-21T01:23:45Z" => "2026-05-21 01:23:45 UTC"
  try {
    return iso.replace('T', ' ').replace('Z', '') + ' UTC';
  } catch(e) { return iso; }
}

/* YYYYMMDD + offset days (UTC) => YYYYMMDD. */
function _gmnShiftYmd(ymd, off) {
  const y = +ymd.slice(0, 4), m = +ymd.slice(4, 6), d = +ymd.slice(6, 8);
  const dt = new Date(Date.UTC(y, m - 1, d));
  dt.setUTCDate(dt.getUTCDate() + (off || 0));
  return dt.toISOString().slice(0, 10).replace(/-/g, '');
}

/* GMN station codes we operate. RO* is a wildcard (we run the whole RO
   prefix). DEZ0DQ/DEZ0S7/DEZ132 are our Berlin nodes — other DE codes belong to
   unrelated German stations and don't link anywhere. Keep in sync with
   gmn_data.DE_STATION_CODES. */
const _GMN_DE_WITNESS_CODES = new Set(['DE0018', 'DE001B', 'DE001C', 'DE001D']);

function _gmnIsOurWitness(code) {
  return code.startsWith('RO') || _GMN_DE_WITNESS_CODES.has(code);
}

function _gmnStationList(stations, ymd, hostByCode) {
  if (!stations || !stations.length) return '—';
  return stations.map(s => {
    const sHtml = escHtml(s);
    if (!_gmnIsOurWitness(s)) return sHtml;
    const host = hostByCode && hostByCode[s];
    if (!host) return `<b>${sHtml}</b>`;  // ours but not in our dashboard (shouldn't happen)
    // Deep-link to the station's RMS / Detection tab, pre-selected to this
    // camera and scrolled to the relevant night. Even if no local clip is
    // matched, the user can browse that night's locked detections directly.
    const href = `/station/${encodeURIComponent(host)}/rms?cam=${encodeURIComponent(s)}&date=${encodeURIComponent(ymd)}`;
    return `<a href="${escHtml(href)}" target="_blank" class="gmn-witness-link"><b>${sHtml}</b></a>`;
  }).join(', ');
}

/* Compact "1.23" / "12.3" / "—" formatter. Drops trailing zeros at given
   precision and surfaces "—" for null / NaN so the popup grid is uniform. */
function _gmnFmt(v, digits) {
  if (v == null || Number.isNaN(v)) return '—';
  return Number(v).toFixed(digits ?? 2);
}

function _gmnPopupHtml(ev, localMatches, ymd, hostByCode) {
  const shower = escHtml(ev.shower || 'Sporadic');
  const mag = ev.peak_mag != null ? ev.peak_mag.toFixed(1) : '—';
  // Entry speed first (what the meteor hit the atmosphere at), then the
  // geocentric Vg the orbit is computed from; Vg alone reads as impossible for
  // a slow meteoroid (4.7 km/s against the 11.2 km/s minimum entry speed).
  const vel = [
    ev.velocity_init != null ? 'entry ' + ev.velocity_init.toFixed(1) : null,
    ev.velocity      != null ? 'geo '   + ev.velocity.toFixed(1)      : null,
  ].filter(Boolean).join(' · ') || '—';
  const altB = ev.altitude_begin != null ? ev.altitude_begin.toFixed(1) : '—';
  const altE = ev.altitude_end   != null ? ev.altitude_end.toFixed(1)   : '—';

  // Orbital elements (heliocentric) + geocentric radiant.
  // Only render the orbit block when we actually have at least one of
  // these values — keeps the popup tight on single-station / no-fit
  // events where GMN didn't compute a full orbit.
  let orbitBlock = '';
  if (ev.orbit_a != null || ev.orbit_e != null || ev.orbit_i != null) {
    const a    = _gmnFmt(ev.orbit_a, 2);
    const e    = _gmnFmt(ev.orbit_e, 3);
    const i    = _gmnFmt(ev.orbit_i, 1);
    const peri = _gmnFmt(ev.orbit_peri, 1);
    const node = _gmnFmt(ev.orbit_node, 1);
    const q    = _gmnFmt(ev.orbit_q, 3);
    const ra   = _gmnFmt(ev.ra_geo, 1);
    const dec  = _gmnFmt(ev.dec_geo, 1);
    orbitBlock =
      `<div style="margin-top:6px;padding-top:6px;border-top:1px solid var(--border);font-size:11px;line-height:1.6">` +
      `<div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.06em;margin-bottom:2px">Orbit</div>` +
      `<div>Radiant: <b>RA ${ra}&deg; Dec ${dec}&deg;</b></div>` +
      `<div>a = <b>${a}</b> AU &middot; e = <b>${e}</b> &middot; i = <b>${i}&deg;</b></div>` +
      `<div>&omega; = <b>${peri}&deg;</b> &middot; &Omega; = <b>${node}&deg;</b> &middot; q = <b>${q}</b> AU</div>` +
      `</div>`;
  }
  // Local matches: all matching clips side-by-side in a horizontal flex row
  // (scrolls horizontally if more clips than fit). Each clip has its own
  // station link above the video player.
  let localBlock = '';
  if (localMatches && localMatches.length) {
    const cells = localMatches.map(m => {
      // ?format=mp4: Safari and iOS cannot play the MKV originals.
      const videoUrl = (m.source === 'archive'
        ? `/api/archive/file/${encodeURIComponent(m.cam)}/${m.date}/meteors/${encodeURIComponent(m.filename)}`
        : `/video/${encodeURIComponent(m.host)}/${encodeURIComponent(m.cam)}/${m.date}/${encodeURIComponent(m.filename)}`) + '?format=mp4';
      const stationUrl = `/station/${encodeURIComponent(m.host)}/rms?cam=${encodeURIComponent(m.cam)}&date=${encodeURIComponent(m.date)}`;
      // window = [offset - 2 s, offset + duration + 3 s], clamped to 0.
      const winStart = Math.max(0, m.offset_s - 2);
      const winEnd   = m.offset_s + m.duration_s + 3;
      const clipLabel = `${escHtml(m.host)} / ${escHtml(m.cam)}`;
      return `<div class="gmn-clip">
        <div class="gmn-clip-label">
          <a href="${escHtml(stationUrl)}" target="_blank">${clipLabel}</a>
        </div>
        <video src="${escHtml(videoUrl)}#t=${winStart},${winEnd}" controls preload="auto" muted playsinline
               data-win-start="${winStart}" data-win-end="${winEnd}"
               data-video-url="${escHtml(videoUrl)}"
               data-station-url="${escHtml(stationUrl)}"
               data-host="${escHtml(m.host)}" data-cam="${escHtml(m.cam)}"
               data-date="${escHtml(m.date)}" data-filename="${escHtml(m.filename)}"
               data-offset="${escHtml(String(m.offset_s ?? ''))}"
               data-mtime="${escHtml(m.meteor_time || '')}"
               data-clip-label="${clipLabel}"
               title="Click to enlarge"
               onclick="_gmnMaybeOpenClipModal(event,this)"
               class="gmn-sync-video"></video>
      </div>`;
    }).join('');
    localBlock =
      `<div style="display:flex;align-items:center;gap:8px;margin-top:6px">` +
      `<div style="font-size:10px;color:var(--muted)">ROVIMEN local clip${localMatches.length > 1 ? 's' : ''} (${localMatches.length}) — −2s before to +3s after meteor</div>` +
      `<button class="gmn-sync-btn" onclick="_gmnTogglePlayAll(this)" style="margin-left:auto;background:var(--blue);color:#0d1117;border:none;border-radius:3px;padding:3px 10px;font-size:11px;font-weight:700;cursor:pointer;font-family:inherit">&#9654; Play all</button>` +
      `</div><div class="gmn-clips">${cells}</div>`;
  }
  return `<div style="font-family:'SF Mono',Consolas,monospace;font-size:12px;line-height:1.6;color:var(--text)">` +
    `<div style="margin-bottom:4px;font-weight:700;color:#e6edf3">${escHtml(_gmnFormatTime(ev.time))}</div>` +
    `<div>Shower: <b>${shower}</b></div>` +
    `<div>Peak mag: <b>${mag}</b></div>` +
    `<div>Velocity: <b>${vel} km/s</b></div>` +
    `<div>Altitude: <b>${altB} &rarr; ${altE} km</b></div>` +
    orbitBlock +
    `<div style="margin-top:4px;font-size:11px;line-height:1.5">Stations: ${_gmnStationList(ev.stations, ymd, hostByCode)}</div>` +
    localBlock +
    `</div>`;
}

/* Build a host -> our station codes lookup from /api/stations, used by
   _gmnMatchLocalChunks to translate a GMN station code (e.g. RO000A) into
   our dashboard's host_key. Cached at module scope so we don't refetch it
   every popup. */
let _gmnHostForCode = null;
async function _gmnBuildHostMap() {
  if (_gmnHostForCode) return _gmnHostForCode;
  _gmnHostForCode = {};
  try {
    const meta = await fetchJson('/api/stations');
    for (const [host, m] of Object.entries(meta || {})) {
      for (const c of (m.cameras || [])) {
        if (c.code) _gmnHostForCode[c.code] = host;
      }
    }
  } catch(e) {}
  return _gmnHostForCode;
}

/* Find local chunks whose meteor_time falls within +/-2 s of the GMN event
   begin time. detectionsByDate is a {ymd: payload} map covering BOTH the
   GMN UTC date and the previous one, because color_capture's night folder
   rolls over at noon UTC — a 01:14 UTC meteor on May 21 lives in the
   20260520/ folder. eventStations is the GMN witness list. */
function _gmnMatchLocalChunks(ev, detectionsByDate, hostMap) {
  if (!detectionsByDate || !ev.time) return [];
  // Normalize event time to ms.
  const t = Date.parse(ev.time.endsWith('Z') ? ev.time : ev.time.replace(' ', 'T') + 'Z');
  if (Number.isNaN(t)) return [];
  const out = [];
  for (const code of ev.stations || []) {
    const host = hostMap[code];
    if (!host) continue;
    // Walk both candidate night folders and emit any chunk whose meteor
    // time lands within ±10 s of GMN's triangulated event time. The old
    // ±2 s window was too tight: RMS records detections at FF block
    // boundaries (~10 s granularity) and GMN's solver rounds its
    // emitted time, so a meaningful fraction of true matches sit
    // 3-10 s apart even when the same physical meteor is the subject.
    // The same chunk can't exist in two date folders so dedup isn't
    // needed.
    for (const [ymd, data] of Object.entries(detectionsByDate)) {
      const st = data && data.by_station && data.by_station[host];
      if (!st || !st.cameras) continue;
      const chunks = st.cameras[code] || [];
      for (const c of chunks) {
        if (!c.meteor_time) continue;
        // Clip-pending detections carry filename: null. The popup builds a
        // <video src> straight from filename, so emitting one produces a
        // witness row with a dead player instead of no row at all — the same
        // filter the event modal applies to witnesses.
        if (!c.filename) continue;
        const ct = Date.parse(c.meteor_time.endsWith('Z') ? c.meteor_time : c.meteor_time + 'Z');
        if (Number.isNaN(ct)) continue;
        if (Math.abs(ct - t) > 10000) continue;
        // detection_offset_s = seconds into the clip where the meteor begins.
        // duration_s (from RMS metadata) defaults to a safe 0.5 s.
        const offset = (typeof c.detection_offset_s === 'number') ? c.detection_offset_s : 0;
        const dur = (c.rms && typeof c.rms.duration_s === 'number') ? c.rms.duration_s : 0.5;
        out.push({
          host, cam: code, date: ymd,
          filename: c.filename, meteor_time: c.meteor_time,
          offset_s: offset, duration_s: dur,
          source: c.source || 'station',
        });
      }
    }
  }
  return out;
}

/* Tracks the timeupdate listeners so we can detach them when a popup closes. */
let _gmnSyncListeners = [];

function _gmnAttachSyncedPlayback(e) {
  const popupEl = e.popup && e.popup.getElement();
  if (!popupEl) return;
  const videos = Array.from(popupEl.querySelectorAll('video.gmn-sync-video'));
  if (!videos.length) return;
  // Each video has its own meteor window. Seek + play simultaneously.
  // Autoplay only works while muted — controls remain visible so the user
  // can unmute / scrub manually if they want sound or a different range.
  for (const v of videos) {
    const winStart = parseFloat(v.dataset.winStart || '0');
    const winEnd   = parseFloat(v.dataset.winEnd   || '0');
    const onTimeUpdate = () => {
      if (winEnd > 0 && v.currentTime >= winEnd) {
        v.pause();
        v.currentTime = winStart;  // rewind so a second click on Play replays cleanly
      }
    };
    v.addEventListener('timeupdate', onTimeUpdate);
    _gmnSyncListeners.push([v, onTimeUpdate]);
    // Surface 404 / decode errors inline so a broken clip shows a useful
    // message instead of a black box that quietly does nothing.
    const onError = () => {
      v.dataset.gmnErrored = '1';
      const cell = v.closest('.gmn-clip');
      if (cell && !cell.querySelector('.gmn-clip-error')) {
        const msg = document.createElement('div');
        msg.className = 'gmn-clip-error';
        msg.textContent = 'Clip unavailable';
        cell.appendChild(msg);
      }
    };
    v.addEventListener('error', onError, { once: true });
    _gmnSeekAndPlayOne(v);
  }
}

function _gmnPausePopupVideos(e) {
  const popupEl = e.popup && e.popup.getElement();
  if (popupEl) popupEl.querySelectorAll('video').forEach(v => v.pause());
  // Detach timeupdate listeners so they don't leak across popup opens.
  for (const [v, fn] of _gmnSyncListeners) v.removeEventListener('timeupdate', fn);
  _gmnSyncListeners = [];
}

/* Seek one video to its window start and resume playback. Defers the seek
   until the element has metadata so a click that lands before the file is
   loaded actually takes effect (previous version swallowed the
   InvalidStateError from currentTime= and then play()'d from t=0). */
function _gmnSeekAndPlayOne(v) {
  if (v.dataset.gmnErrored === '1') return;            // bad clip; don't fight it
  const ws = parseFloat(v.dataset.winStart || '0');
  const seek = () => {
    try { v.currentTime = ws; } catch (_) {}
    const p = v.play();
    if (p && typeof p.catch === 'function') {
      p.catch(err => {
        // Autoplay block (NotAllowedError) is the realistic failure case for
        // a synthetic click chain. Surface it so the user knows to use the
        // native controls rather than wondering why nothing happens.
        console.warn('[gmn] play() rejected:', err && err.name, err && err.message);
      });
    }
  };
  if (v.readyState >= 1) seek();
  else v.addEventListener('loadedmetadata', seek, { once: true });
}

/* ── Enlarged clip modal (D3b) ─────────────────────────────────────────
   Click a witness clip in the popup → open the same clip in a large
   modal player. Mirrors the per-station detection-page modal but kept
   self-contained on this page (no dashboard-station.js dependency).
   Note: native <video> controls eat their own click for play/pause, so
   we only open the modal on clicks outside the controls strip — checked
   by event.offsetY < (videoHeight - controls strip). Place rotate(...)
   as the rightmost / first-applied op so user pan operates in screen
   coordinates rather than the rotated image frame. */
window._gmnMaybeOpenClipModal = function(ev, vidEl) {
  // Browsers don't expose the controls hit area directly, but the strip
  // is ~32 px tall along the bottom. Skip the modal open if the click
  // landed in that band so the native Play / Mute / scrub controls work
  // as-expected; otherwise (click on the video frame itself) enlarge.
  const rect = vidEl.getBoundingClientRect();
  const localY = ev.clientY - rect.top;
  if (rect.height > 0 && localY > rect.height - 40) return;
  ev.preventDefault();
  ev.stopPropagation();
  _gmnOpenClipModal(vidEl);
};

// GMN clip modal — shared VideoModal instance (no trim, no nav)
const _gmnClipModal = new VideoModal(
  document.getElementById('gmn-clip-modal-container'),
  { trim: false, nav: false }
);
window._VideoModalCloseAll = () => VideoModal.closeAll();

window._gmnOpenClipModal = function(vidEl) {
  const url      = vidEl.dataset.videoUrl;
  const winStart = parseFloat(vidEl.dataset.winStart || '0');
  const winEnd   = parseFloat(vidEl.dataset.winEnd   || '0');
  const label    = vidEl.dataset.clipLabel  || '';
  const stationUrl = vidEl.dataset.stationUrl || '';
  if (!url) return;
  // Pause every inline witness video so the modal player gets the focus.
  document.querySelectorAll('.gmn-sync-video').forEach(v => v.pause());
  // Build title — keep the station link if available
  const titleHtml = stationUrl
    ? `<a href="${escHtml(stationUrl)}" target="_blank" style="color:inherit">${escHtml(label)}</a>`
    : escHtml(label);
  // Use #t=start,end fragment so the browser seeks automatically.
  // station/camera/date/filename give the modal its "Open station / Video
  // DB" links and "Download clip" hands out the original (archive, else
  // station) via /download/.
  const { host, cam, date, filename, offset, mtime } = vidEl.dataset;
  const known = !!(host && cam && date && filename);
  _gmnClipModal.open({
    src:   `${url}#t=${winStart},${winEnd}`,
    title: label,
    station: host || '', camera: cam || '', date: date || '', filename: filename || '',
    detOffset: offset !== undefined && offset !== '' ? parseFloat(offset) : null,
    download: known && /\.mkv$/.test(filename)
      ? { originalUrl: `/download/${encodeURIComponent(host)}/${encodeURIComponent(cam)}/${encodeURIComponent(date)}/${encodeURIComponent(filename)}` }
      : null,
    // Hide as a false positive (dashboard-hidden.js; shown only to sessions
    // that may hide). The popup lists only detections that are not hidden.
    hide: cam && mtime ? { target: hideTarget({ meteor_time: mtime }, cam), hidden: false, page: 'overview' } : null,
  });
  // Overwrite title span innerHTML to support the station hyperlink
  const titleEl = _gmnClipModal._el?.querySelector('.vm-title');
  if (titleEl) titleEl.innerHTML = titleHtml;
};

window._gmnCloseClipModal = function() {
  _gmnClipModal.close();
};

/* Play-all button: re-seek every video to its window start and play them
   together. Useful after the auto-play first loop has paused. */
window._gmnTogglePlayAll = function(btn) {
  const popupEl = btn.closest('.leaflet-popup-content');
  if (!popupEl) return;
  const videos = Array.from(popupEl.querySelectorAll('video.gmn-sync-video'))
    .filter(v => v.dataset.gmnErrored !== '1');
  if (!videos.length) return;
  // Toggle: if any video is currently playing, pause everything; else seek
  // all to their window starts and play together.
  const playing = videos.some(v => !v.paused);
  if (playing) {
    videos.forEach(v => v.pause());
    btn.innerHTML = '&#9654; Play all';
  } else {
    videos.forEach(_gmnSeekAndPlayOne);
    btn.innerHTML = '&#10073;&#10073; Pause';
  }
};

async function _gmnFetchAndDraw(date) {
  const banner    = document.getElementById('gmn-banner');
  const dateInput = document.getElementById('gmn-date-input');
  if (!_gmnActive) return;

  if (banner) {
    banner.style.display = 'block';
    _gmnSetBanner(banner, { text: 'Loading GMN data…' });
  }

  if (!await _gmnMapReady()) {
    _gmnSetBanner(banner, { text: 'Map is still loading — try the layer again', tone: 'warn' });
    return;
  }
  if (!_gmnActive) return;   // toggled back off while the map was building

  // Clear existing layer
  if (_gmnLayer) { map.removeLayer(_gmnLayer); _gmnLayer = null; }
  _gmnLayer = L.layerGroup().addTo(map);

  // Fan out fetches in parallel: GMN events + local detections + cam-code map.
  // Local detections are fetched for a 4-day window centered on the picked
  // night (-2 ... +1 day in UTC) so chunks from the night before / after
  // are also available — meteor_time on the GMN side is the trajectory
  // begin UTC, which can sit on either calendar day around midnight, and
  // the storage-box archive fallback in compute_detections_payload makes
  // older dates still match if the station has long-rolled the locked
  // chunk off local disk.
  const ymd = date.replace(/-/g, '');
  const dateRange = [-2, -1, 0, 1].map(off => _gmnShiftYmd(ymd, off));
  const fetches = [
    fetchJson(`/api/gmn/multistation/${date}`),
    _gmnBuildHostMap(),
    ...dateRange.map(d => fetchJson(`/api/detections/${d}`).catch(() => null)),
  ];
  const results = await Promise.allSettled(fetches);
  const gmnRes = results[0];
  const hostMap = results[1];
  const data = gmnRes.status === 'fulfilled' ? gmnRes.value : null;
  const hostByCode = hostMap.status === 'fulfilled' ? hostMap.value : {};
  const detectionsByDate = {};
  for (let i = 0; i < dateRange.length; i++) {
    const r = results[i + 2];
    if (r.status === 'fulfilled' && r.value) detectionsByDate[dateRange[i]] = r.value;
  }

  if (!data) {
    _gmnSetBanner(banner, { text: 'GMN data unavailable — network error', tone: 'warn' });
    return;
  }

  if (data.error) {
    _gmnSetBanner(banner, {
      text: 'GMN upstream unavailable — retrying every 2 h',
      title: data.error,
    });
    return;
  }
  if (!Array.isArray(data.events) || !data.events.length) {
    _gmnSetBanner(banner, _gmnEmptyNotice(data));
    return;
  }

  const blueColor = '#388bfd';
  const grayColor = '#6e7681';

  // Magnitude-scaled traces — bumped contrast pass.
  // Brighter meteors (more negative absolute magnitude) get thicker, more
  // opaque polylines. Width range is now 2..10 px (was 1..6) so the
  // brightest events really pop on the map. Clamp peak_mag to [-5, +5].
  // Sporadic / null mag falls back to a sensible mid weight so events
  // without GMN photometry stay visible. DE-only events keep the muted
  // overlay treatment but still scale.
  const _gmnTraceFromMag = function(peakMag, deOnly) {
    if (peakMag == null || Number.isNaN(peakMag)) {
      return deOnly ? { weight: 2, opacity: 0.40 }
                    : { weight: 3, opacity: 0.85 };
    }
    const clamped = Math.max(-5, Math.min(5, peakMag));
    const t = (clamped + 5) / 10;            // 0 (bright) .. 1 (dim)
    const widthBright = 10, widthDim = 2;
    const w = Math.max(2, widthDim + (widthBright - widthDim) * (1 - t));
    const a = deOnly ? (0.20 + 0.45 * (1 - t))   // 0.20 dim .. 0.65 bright
                     : (0.45 + 0.55 * (1 - t));  // 0.45 dim .. 1.00 bright
    return { weight: w, opacity: a };
  };

  /* Direction arrow — small triangle marker at the trace's end point
     rotated to the bearing from begin → end so users can see which way
     the meteor flew without inferring it from the dot. Falls back to no
     arrow if either endpoint is missing (defensive — the parent guard
     already filters those events out). */
  const _gmnArrowMarker = function(latlngBegin, latlngEnd, color, opacity, weight) {
    const [lat1, lon1] = latlngBegin;
    const [lat2, lon2] = latlngEnd;
    // Spherical bearing in degrees clockwise from north.
    const φ1 = lat1 * Math.PI / 180, φ2 = lat2 * Math.PI / 180;
    const Δλ = (lon2 - lon1) * Math.PI / 180;
    const y = Math.sin(Δλ) * Math.cos(φ2);
    const x = Math.cos(φ1) * Math.sin(φ2) - Math.sin(φ1) * Math.cos(φ2) * Math.cos(Δλ);
    const bearing = (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
    // Scale arrow with line weight so brighter (thicker) traces also get
    // a beefier arrowhead. Clamp so it's never microscopic on dim events.
    const size = Math.max(10, Math.round(weight * 1.8 + 6));
    const half = size / 2;
    const icon = L.divIcon({
      className: 'gmn-arrow',
      html: `<svg width="${size}" height="${size}" viewBox="0 0 20 20"
                  style="transform:rotate(${bearing}deg);transform-origin:50% 50%;display:block">
               <polygon points="10,1 18,17 10,13 2,17"
                        fill="${color}" fill-opacity="${opacity}"
                        stroke="#0d1117" stroke-width="1.2"/>
             </svg>`,
      iconSize: [size, size],
      iconAnchor: [half, half],
    });
    return L.marker(latlngEnd, { icon, interactive: false, keyboard: false });
  };

  for (const ev of data.events) {
    if (ev.lat_begin == null || ev.lon_begin == null ||
        ev.lat_end   == null || ev.lon_end   == null) continue;

    const color   = ev.de_only ? grayColor : blueColor;
    const { weight, opacity } = _gmnTraceFromMag(ev.peak_mag, ev.de_only);

    const localMatches = _gmnMatchLocalChunks(ev, detectionsByDate, hostByCode);
    // Popup width grows when we have an embedded video player.
    const popup     = _gmnPopupHtml(ev, localMatches, ymd, hostByCode);
    // 50 % bigger than before (was 320 / 260) so single-column clip stack
    // and metadata text breathe — readable on phone landscape too.
    const popupOpts = { maxWidth: localMatches.length ? 480 : 390 };

    const latlngs = [[ev.lat_begin, ev.lon_begin], [ev.lat_end, ev.lon_end]];

    // Invisible thick hitbox under the visible polyline so clicking
    // anywhere within ~12 px of the track opens the popup — the visible
    // 1.5-2.5 px line is way too narrow to hit reliably.
    const hitbox = L.polyline(latlngs, {
      color: '#000', weight: 14, opacity: 0,
      interactive: true, bubblingMouseEvents: false,
    }).bindPopup(popup, { ...popupOpts, className: 'gmn-popup' });

    const line = L.polyline(latlngs, {
      color, weight, opacity,
      interactive: false,
    });

    const dot = L.circleMarker([ev.lat_begin, ev.lon_begin], {
      radius: 4, color, fillColor: color, fillOpacity: 0.9,
      weight: 1, opacity
    }).bindPopup(popup, { ...popupOpts, className: 'gmn-popup' });

    _gmnLayer.addLayer(hitbox);
    _gmnLayer.addLayer(line);
    _gmnLayer.addLayer(dot);
    _gmnLayer.addLayer(_gmnArrowMarker(latlngs[0], latlngs[1], color, opacity, weight));
  }

  // Auto-sync videos when a popup opens: seek each to its win-start, then
  // play them simultaneously (muted, the only way browsers permit autoplay).
  // Each video pauses itself when it crosses win-end so the clips loop
  // through their meteor window cleanly.
  _gmnLayer.on('popupopen', _gmnAttachSyncedPlayback);
  _gmnLayer.on('popupclose', _gmnPausePopupVideos);

  // Fetch monthly count for the selected date's month
  let monthCount = null;
  const selectedMonth = date.slice(0, 7);
  try {
    const mdata = await fetchJson(`/api/gmn/monthly_count?month=${selectedMonth}`);
    monthCount = mdata.ro_orbit_count;
  } catch(e) {}

  if (banner) {
    let text = `Showing ${data.events.length} GMN orbits for ${date}`;
    if (monthCount != null) {
      const now = new Date();
      const isCurrentMonth = selectedMonth === `${now.getUTCFullYear()}-${String(now.getUTCMonth()+1).padStart(2,'0')}`;
      const label = isCurrentMonth ? 'This month' : selectedMonth;
      text = `${label} RO cameras contributed to the calculation of ${monthCount} orbits`;
    }
    _gmnSetBanner(banner, { text });
  }
}

function toggleGmnLayer(btn) {
  _gmnActive = !_gmnActive;
  setPressed(btn, _gmnActive);
  localStorage.setItem('ov_gmn_layer', _gmnActive ? '1' : '0');

  const dateRow   = document.getElementById('gmn-date-row');
  const banner    = document.getElementById('gmn-banner');

  if (_gmnActive) {
    if (dateRow) dateRow.style.display = 'flex';
    if (banner) {
      banner.style.display = 'block';
      _gmnSetBanner(banner, { text: 'Loading GMN data…' });
    }
    _gmnOpenLayer();
  } else {
    if (_gmnLayer) { map.removeLayer(_gmnLayer); _gmnLayer = null; }
    if (dateRow) dateRow.style.display = 'none';
    if (banner)  banner.style.display  = 'none';
  }
}

function onGmnDateChange(val) {
  if (!_gmnActive || !val) return;
  _gmnFetchAndDraw(val);
}

let map;
function initMap(data) {
  mapData=data;
  map=L.map('map',{zoomControl:false,scrollWheelZoom:true,zoomSnap:0.25,zoomDelta:0.25,wheelPxPerZoomLevel:200});
  // Fit to all visible stations on first load so the fleet is in view
  // regardless of where future stations land. Falls back to a Europe-wide
  // view if no station has coords yet.
  const _bpts = [];
  for (const [k, info] of Object.entries(data || {})) {
    const lat = info.lat ?? STATION_COORDS[k]?.lat;
    const lon = info.lon ?? STATION_COORDS[k]?.lon;
    if (lat == null || lon == null || info.show_on_map === false) continue;
    _bpts.push([lat, lon]);
  }
  if (_bpts.length >= 2) {
    map.fitBounds(L.latLngBounds(_bpts), {padding: [40, 40], maxZoom: 7});
  } else if (_bpts.length === 1) {
    map.setView(_bpts[0], 7);
  } else {
    map.setView([46.5, 25.5], 6);
  }
  L.control.zoom({position: window.innerWidth <= 768 ? 'bottomright' : 'topright'}).addTo(map);
  // Custom pane for LP overlay — sits above tiles but not dimmed by tile brightness filter
  map.createPane('lpPane');
  // Restore saved layer + brightness
  const savedLayer = localStorage.getItem('ov_map_layer') || 'std';
  const savedBrightness = parseInt(localStorage.getItem('ov_map_brightness') || '50', 10);
  const brightnessSlider = document.getElementById('map-brightness');
  if (brightnessSlider) brightnessSlider.value = savedBrightness;
  const savedLayerBtn = document.getElementById(`ml-${savedLayer}`);
  if (savedLayerBtn) {
    document.querySelectorAll(_BASE_LAYER_BTNS).forEach(b => setPressed(b, b === savedLayerBtn));
  }
  const {url, opts} = MAP_LAYERS[savedLayer] || MAP_LAYERS.std;
  _tileLayer = L.tileLayer(url, opts).addTo(map);
  _applyRefLayer(savedLayer);
  const brightnessRow = document.getElementById('brightness-row');
  if (savedLayer === 'short' || savedLayer === 'sat') {
    if (brightnessRow) brightnessRow.classList.add('hidden');
  } else {
    document.querySelector('.leaflet-tile-pane') && (document.querySelector('.leaflet-tile-pane').style.filter = `brightness(${savedBrightness/100})`);
  }

  // Restore LP overlay state
  if (localStorage.getItem('ov_lp_layer') === '1') {
    _lpLayer = L.tileLayer(LP_TILE_URL, LP_OPTS).addTo(map);
    const lpBtn = document.getElementById('ml-lp');
    setPressed(lpBtn, true);
  }

  // Restore GMN orbit layer state
  if (localStorage.getItem('ov_gmn_layer') === '1') {
    _gmnActive = true;
    const gmnBtn    = document.getElementById('ml-gmn');
    const dateRow   = document.getElementById('gmn-date-row');
    setPressed(gmnBtn, true);
    if (dateRow) dateRow.style.display = 'flex';
    // defer draw until after map is fully set up (footprintLayer etc.)
    setTimeout(() => { _gmnOpenLayer(); }, 0);
  }

  footprintLayer=L.layerGroup().addTo(map);
  drawFootprints(data, FOV_ALT_KM);

  map.on('click', e=>{
    if (_pickMode) { _openPointPicker(e.latlng); return; }
    closePanel();
  });

  // Zoomed out, the Romanian stations sit a few km apart, so the name labels
  // drop the station ID (it stays in the hover title) to overlap less.
  const syncLabelDetail = () => map.getContainer().classList.toggle('ov-labels-compact', map.getZoom() < 7);
  map.on('zoomend', syncLabelDetail);
  syncLabelDetail();
  // Station dots and names double in size every two zoom levels in (0.6x to
  // 2.4x around zoom 7), so they stay readable as the map zooms. Below zoom
  // 5.5 the whole network fits in a few hundred px and the names would only
  // overlap, so they hide and show on hover.
  const syncMarkerScale = z => {
    const c = map.getContainer();
    c.style.setProperty('--ov-marker-scale', _markerScaleAt(z).toFixed(3));
    c.classList.toggle('ov-labels-hidden', z < 5.5);
    c.classList.toggle('ov-collapsed', z < 5);
  };
  map.on('zoomanim', e => syncMarkerScale(e.zoom));
  map.on('zoomend', () => syncMarkerScale(map.getZoom()));
  syncMarkerScale(map.getZoom());
  // Stations a few pixels apart fan out and their names pick free sides;
  // registered after syncMarkerScale so the layout reads the new scale.
  map.on('zoomend moveend resize', _scheduleMarkerLayout);

  let i=0;
  for(const [key,info] of Object.entries(data)){
    const lat=info.lat??STATION_COORDS[key]?.lat;
    const lon=info.lon??STATION_COORDS[key]?.lon;
    if(!lat||!lon||info.show_on_map===false){i++;continue}
    const online=info.online;
    const isCommissioning = info.status === 'commissioning';
    const dotColor = isCommissioning ? '#e89b3c' : (online ? '#3fb950' : '#f85149');
    const place = info.label || key;
    // The name sits beside the dot; the icon box stays the dot so the anchor
    // is the station position. The body (dot + name) is what moves when
    // _applyMarkerLayout fans out stations that sit on top of each other;
    // the leader line then runs from the true position to the dot.
    const icon=L.divIcon({
      className:'ov-marker',
      html:`<div class="ov-marker-leader" hidden></div><div class="ov-marker-body">`
        + `<div class="ov-marker-dot" style="width:14px;height:14px;border-radius:50%;
        background:${dotColor};
        border:2px solid #0d1117;box-shadow:0 0 6px ${dotColor}"></div>`
        + `<span class="ov-marker-name" role="link">`
        + `${escHtml(place)}<span class="ov-marker-id">${escHtml(key)}</span></span></div>`,
      iconSize:[MARKER_DOT_PX,MARKER_DOT_PX],iconAnchor:[MARKER_DOT_PX/2,MARKER_DOT_PX/2],
    });
    const marker=L.marker([lat,lon],{icon}).addTo(map);
    stationMarkers[key] = marker;
    stationInfoByKey[key] = info;
    // Hover card: cameras and GMN orbits. Built on open, so it shows the
    // counts as soon as /api/overview/station_orbits has answered.
    // A tap opens it on touch screens and hides the map; the band carries the orbits there.
    if (!matchMedia('(hover: none)').matches) marker.bindTooltip(() => stationHoverHtml(key, info, _stationOrbits), {
      direction: 'top', offset: [0, -TIP_LIFT_PX], opacity: 1, className: 'ov-station-tip',
    });
    // Handled here rather than with a native <a>: Leaflet drops the click that
    // ends a map drag, so panning by grabbing a name doesn't navigate.
    marker.on('click', e=>{
      L.DomEvent.stopPropagation(e);
      const oe = e.originalEvent || {};
      if (oe.target?.closest?.('.ov-marker-name') && !oe.shiftKey) {
        // The name opens the station; Ctrl/Cmd-click opens it in a new tab.
        const url = `/station/${encodeURIComponent(key)}`;
        if (oe.ctrlKey || oe.metaKey) window.open(url, '_blank', 'noopener');
        else window.location.href = url;
        return;
      }
      // The dot (or Shift-click on the name) toggles this station's fields of
      // view, adding to any already shown, WITHOUT zooming or recentring the
      // map (issue #511).
      toggleStationSelection(key, info);
      // Re-assert the highlight: Leaflet may have re-rendered the icon element.
      _setMarkerSelected(key, state.selectedStations.has(key));
    });
    i++;
  }
  _addZoomOutCard();
  _scheduleMarkerLayout();
  // The names' widths change when the web font arrives.
  document.fonts?.ready?.then(_scheduleMarkerLayout);
}

// Zoomed far out the dots collapse into one card: station counts by state.
// Clicking it zooms back to the network.
function _addZoomOutCard() {
  const counts = { online: 0, commissioning: 0, offline: 0 };
  const pts = [];
  for (const [key, m] of Object.entries(stationMarkers)) {
    const info = stationInfoByKey[key];
    counts[info.status === 'commissioning' ? 'commissioning' : info.online ? 'online' : 'offline']++;
    pts.push(m.getLatLng());
  }
  const card = document.createElement('button');
  card.className = 'ov-zoomout-card';
  card.title = 'Zoom to the network';
  card.innerHTML = [['online', '#3fb950'], ['commissioning', '#e89b3c'], ['offline', '#f85149']]
    .map(([k, c]) => `<span><i style="background:${c}"></i><b>${counts[k]}</b> ${k}</span>`).join('');
  card.onclick = () => map.fitBounds(L.latLngBounds(pts), { padding: [40, 40], maxZoom: 7 });
  L.DomEvent.disableClickPropagation(card);
  map.getContainer().appendChild(card);
}

// ── Station marker layout (overlapping stations) ────────────────────────────
// Dot size at scale 1 (the CSS scales it by --ov-marker-scale), the gap
// between a dot's edge and its name (.ov-marker-name `left`: 10px + 7px*s),
// and how far above the dot the hover card sits.
const MARKER_DOT_PX = 14;
const MARKER_NAME_GAP_PX = 3;
const TIP_LIFT_PX = 10;
// --ov-marker-scale at zoom z (see syncMarkerScale in initMap).
const _markerScaleAt = z => Math.min(2.4, Math.max(0.6, 2 ** ((z - 7) / 2)));

let _markerLayoutRaf = 0;
function _scheduleMarkerLayout() {
  if (_markerLayoutRaf) return;
  _markerLayoutRaf = requestAnimationFrame(() => { _markerLayoutRaf = 0; _applyMarkerLayout(); });
}

// Measure every station's screen position and name, lay them out
// (dashboard-marker-layout.js), and move each body + name side. The marker
// itself stays on the true position, so fields of view, the point picker and
// popups are unaffected; the click and hover handlers sit on the marker and
// fire from the moved dot and name.
function _applyMarkerLayout() {
  if (!map) return;
  const s = _markerScaleAt(map.getZoom());
  const named = !map.getContainer().classList.contains('ov-labels-hidden');
  const items = [];
  for (const [key, marker] of Object.entries(stationMarkers)) {
    const el = marker.getElement?.();
    if (!el) continue;
    const name = named ? el.querySelector('.ov-marker-name') : null;
    const p = map.latLngToContainerPoint(marker.getLatLng());
    // The name's size at the new scale: its box now, over the scale it is
    // drawn at now (mid-transition after a zoom), so sub-pixel widths count.
    let w = 0;
    let h = 0;
    if (name) {
      const r = name.getBoundingClientRect();
      const cur = parseFloat(getComputedStyle(name).scale) || 1;
      w = (r.width / cur) * s;
      h = (r.height / cur) * s;
    }
    items.push({ key, x: p.x, y: p.y, w, h });
  }
  const layout = layoutStationMarkers(items, { dot: MARKER_DOT_PX * s, gap: MARKER_NAME_GAP_PX });
  for (const { key } of items) _placeMarker(key, layout[key]);
}

function _placeMarker(key, place) {
  const marker = stationMarkers[key];
  const el = marker?.getElement?.();
  if (!el || !place) return;
  const { dx, dy, side } = place;
  const moved = Math.hypot(dx, dy) >= 0.5;
  const body = el.querySelector('.ov-marker-body');
  const name = el.querySelector('.ov-marker-name');
  const leader = el.querySelector('.ov-marker-leader');
  if (body) body.style.translate = moved ? `${dx.toFixed(1)}px ${dy.toFixed(1)}px` : '';
  if (name) name.dataset.side = side;
  if (leader) {
    leader.hidden = !moved;
    if (moved) {
      leader.style.width = `${Math.hypot(dx, dy).toFixed(1)}px`;
      leader.style.rotate = `${Math.atan2(dy, dx).toFixed(4)}rad`;
    }
  }
  const tip = marker.getTooltip?.();
  if (tip) {
    tip.options.offset = [dx, dy - TIP_LIFT_PX];
    if (tip.isOpen?.()) tip.update();
  }
}

// ── Station hover card (cameras + GMN orbits) ───────────────────────────────
let _stationOrbits = null;  // /api/overview/station_orbits payload
let _stationOrbitsTimer = null;

async function _loadStationOrbits() {
  clearTimeout(_stationOrbitsTimer);
  try {
    const r = await fetch('/api/overview/station_orbits');
    if (r.ok) { _stationOrbits = await r.json(); _refreshBandOrbits(); }
  } catch { /* keep the last payload; the card says "loading" without one */ }
  // The server computes in the background: poll until the history is in.
  if (!_stationOrbits?.ready || !_stationOrbits?.history_complete) {
    _stationOrbitsTimer = setTimeout(_loadStationOrbits, 60_000);
  }
}

const _MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

// Pure: HTML for one station's hover card. Exported for unit tests.
export function stationHoverHtml(key, info, orbits) {
  const fmt = n => Number(n).toLocaleString('en-US');
  const nCams = (info.cameras || []).length;
  const rows = [`${nCams} camera${nCams === 1 ? '' : 's'}`];
  if (!orbits?.ready) {
    rows.push('<span class="ov-tip-muted">GMN orbits loading&hellip;</span>');
  } else {
    const o = orbits.stations?.[key] || { orbits_30d: 0, orbits_all: 0, first_month: null };
    const since = o.first_month
      ? ` <span class="ov-tip-muted">since ${_MONTHS[+o.first_month.slice(4, 6) - 1]} ${o.first_month.slice(0, 4)}</span>`
      : '';
    const partial = orbits.history_complete ? '' : ' <span class="ov-tip-muted">(older years loading)</span>';
    rows.push(`GMN orbits, last 30 days: <b>${fmt(o.orbits_30d)}</b>`);
    rows.push(`GMN orbits, all time: <b>${fmt(o.orbits_all)}</b>${since}${partial}`);
  }
  return `<div class="ov-tip-title">${escHtml(info.label || key)} <span class="ov-tip-muted">${escHtml(key)}</span></div>`
    + rows.map(r => `<div>${r}</div>`).join('');
}

// ── Network Status table (merged from /network page) ─────────────────────────
let _nwRows = [];
let _nwSortCol = 'online';
let _nwSortDir = -1;
// Offline smoothing is handled server-side (3-consecutive-failures)

function _nwFmtAge(isoStr) {
  if (!isoStr) return { text: '—', cls: 'na' };
  const dt = new Date(isoStr);
  if (isNaN(dt)) return { text: '—', cls: 'na' };
  const secs = Math.floor((Date.now() - dt) / 1000);
  if (secs < 0) return { text: 'just now', cls: 'fresh' };
  if (secs < 90)  return { text: secs + 's ago', cls: 'fresh' };
  if (secs < 3600) return { text: Math.floor(secs / 60) + 'm ago', cls: 'recent' };
  if (secs < 86400) return { text: Math.floor(secs / 3600) + 'h ago', cls: 'stale' };
  return { text: Math.floor(secs / 86400) + 'd ago', cls: 'old' };
}
function _nwMetricClass(val, warn, crit) {
  if (val == null) return 'na';
  return val >= crit ? 'crit' : val >= warn ? 'warn' : 'ok';
}
function _nwFmtPct(val) {
  if (val == null) return { text: '—', cls: 'na' };
  const n = Math.round(val);
  return { text: n + '%', cls: _nwMetricClass(n, 75, 90) };
}
function _nwFmtTemp(val) {
  if (val == null) return { text: '—', cls: 'na' };
  const n = Math.round(val);
  return { text: n + '°', cls: _nwMetricClass(n, 65, 80) };
}
function _nwFmtDisk(pct) {
  if (pct == null || Number.isNaN(pct)) return { text: '—', cls: 'na' };
  const n = Math.round(pct);
  return { text: n + '%', cls: _nwMetricClass(n, 80, 93) };
}
function _nwCamSvcStatus(camCode, services) {
  if (!services || !camCode) return 'svc-unknown';
  for (const [k, v] of Object.entries(services)) {
    if (k.includes(camCode)) {
      return serviceIsUp(v) ? 'svc-ok' : 'svc-dead';
    }
  }
  return 'svc-unknown';
}

// Small badge shown on the online cell when a stale station's quiet detection
// feed is explained by cloud. Returns null when there's nothing to show, so a
// healthy or clear-but-quiet station gets no badge (and never the ☁ marker).
// `weather` comes from /api/overview: 'overcast' means stale + high cloud
// fraction. Any other/absent value → no badge. Exported for unit tests.
export function _nwWeatherBadge(row) {
  if (!row || !row.detectionsStale) return null;
  if (row.weather === 'overcast') {
    return { icon: '☁', cls: 'overcast', title: 'Overcast — few stars detected last night; the quiet detection feed is weather, not a fault' };
  }
  return null;
}

// Colour-segment cadence badge. When a camera stream corrupts, ffmpeg's
// segment muxer stops honouring -segment_time and cuts ~1 s clips instead of
// 20 s ones (RO000Y once wrote 1543 in a single hour); when the stream stalls
// it leaves multi-minute holes instead. Neither moves any other column in this
// table — services, disk and heartbeat all stay green — which is why both used
// to be found the morning after, with the night already lost.
//
// `segmentAlarm` comes from /api/overview as {camera code: 'storm'|'stalls'|
// 'both'}; the station-side thresholds and the alarm decision both live server
// side, so this only formats. Empty / absent (an older station bundle that
// does not report cadence at all) → null, i.e. no badge. Exported for tests.
export function _nwSegmentBadge(row) {
  const alarms = (row && row.segmentAlarm) || {};
  const codes = Object.keys(alarms).sort();
  if (!codes.length) return null;
  const kinds = codes.map(c => alarms[c]);
  const storm = kinds.some(k => k === 'storm' || k === 'both');
  const stalls = kinds.some(k => k === 'stalls' || k === 'both');
  const kind = storm && stalls ? 'both' : storm ? 'storm' : 'stalls';
  const health = new Map((row.cameras || []).map(c => [c.code, c.segment]));
  const detail = codes.map(code => {
    const h = health.get(code) || {};
    const parts = [];
    if (h.short) parts.push(`${h.short} back-to-back segments`);
    if (h.stalls) parts.push(`${h.stalls} gaps`);
    return `${code}: ${parts.length ? parts.join(', ') : alarms[code]}`;
  });
  return {
    label: SEGMENT_ALARM_TEXT[kind],
    cls: storm ? 'storm' : 'stalls',
    title: `Colour capture cadence is broken in the last hour — ${detail.join('; ')}. `
         + 'Check the camera stream now; tonight’s footage is being lost as this runs.',
  };
}

// ── Camera capture state ────────────────────────────────────────────────
// Each camera in /api/overview carries `capture` (dashboard/capture_state.py):
// state recording | degraded | stalled | idle | no_rms | offline | unknown,
// from the FF files RMS wrote, plus last_night {night, n_ff, expected,
// verdict full | partial | poor | none}. The fleet headline counts cameras
// by it, never by service state: a unit stays "active" in daylight and while
// its camera is off the wire. Commissioning stations are left out.
const _CAPTURE_LIVE = new Set(['recording', 'degraded', 'stalled']);

export function captureSummary(overviewData) {
  const sum = { total: 0, recording: 0, degraded: 0, stalled: 0, idle: 0,
                notReporting: 0, noRms: 0,
                lastNight: { night: null, full: 0, partial: 0, poor: 0, none: 0, known: 0 } };
  for (const info of Object.values(overviewData || {})) {
    if (!info || info.status === 'commissioning') continue;
    for (const c of (info.cameras || [])) {
      sum.total++;
      const cap = c.capture || {};
      const st = cap.state || 'unknown';
      if (st === 'recording') sum.recording++;
      else if (st === 'degraded') sum.degraded++;
      else if (st === 'stalled') sum.stalled++;
      else if (st === 'idle') sum.idle++;
      else if (st === 'no_rms') sum.noRms++;
      else sum.notReporting++;
      const ln = cap.last_night;
      if (ln && ln.verdict && ln.verdict in sum.lastNight) {
        sum.lastNight[ln.verdict]++;
        sum.lastNight.known++;
        sum.lastNight.night = sum.lastNight.night || ln.night;
      } else if (st === 'offline' || st === 'no_rms') {
        sum.lastNight.none++;
      }
    }
  }
  return sum;
}

function _fmtNight(n) {
  return n && n.length === 8 ? `${n.slice(0, 4)}-${n.slice(4, 6)}-${n.slice(6)}` : 'last night';
}

// One line for the Live bar and the live grid. At night: how many record now;
// in daytime (nothing recording anywhere): how last night went.
export function captureHeadline(sum) {
  if (!sum || !sum.total) return 'No cameras';
  const live = sum.recording + sum.degraded + sum.stalled;
  if (live > 0) {
    const rec = sum.recording + sum.degraded;
    const extra = [];
    if (sum.degraded) extra.push(`${sum.degraded} degraded`);
    if (sum.stalled) extra.push(`${sum.stalled} stalled`);
    const off = sum.total - rec - sum.stalled - sum.notReporting;
    if (off) extra.push(`${off} not capturing`);
    if (sum.notReporting) extra.push(`${sum.notReporting} not reporting`);
    return `${rec} of ${sum.total} cameras recording` + (extra.length ? ` (${extra.join(', ')})` : '');
  }
  const ln = sum.lastNight;
  // Stations on a bundle without capture evidence (and stations that stopped
  // reporting) are named as such, not counted as cameras that missed the night.
  if (!ln.known) return 'Daytime: RMS records at night. Capture state not reported yet.';
  const notFull = ln.partial + ln.poor;
  return `Daytime: RMS records at night. Night of ${_fmtNight(ln.night)}: ${ln.full} of ${sum.total} cameras recorded a full night`
    + (notFull ? `, ${notFull} part of it` : '')
    + (sum.notReporting ? `, ${sum.notReporting} not reporting` : '');
}

// Camera dot class and tooltip from its capture state. In daytime the dot
// shows how last night went; at night, what the camera does now.
export function _nwCaptureDot(cap) {
  if (!cap || !cap.state || cap.state === 'unknown') return null;
  const ln = cap.last_night;
  const nightTxt = ln
    ? `Night ${_fmtNight(ln.night)}: ${ln.n_ff}${ln.expected ? ' of ' + ln.expected : ''} FF files`
    : '';
  const parts = [cap.detail, nightTxt].filter(Boolean);
  let cls;
  switch (cap.state) {
    case 'recording': cls = 'svc-ok'; break;
    case 'degraded': cls = 'svc-warn'; break;
    case 'stalled': case 'no_rms': cls = 'svc-dead'; break;
    case 'offline': cls = 'svc-unknown'; break;
    default: {  // idle: judge by last night
      const v = ln && ln.verdict;
      cls = v === 'full' ? 'svc-ok' : v === 'partial' ? 'svc-warn' : v ? 'svc-dead' : 'svc-unknown';
    }
  }
  return { cls, title: parts.join('. ') };
}

// Per-camera sky marker from the latest night's star counts (share of FF
// frames with too few stars, detection_indexer.py). Every camera with a sky
// row gets one, so a quiet camera reads as weather or as a fault at a glance.
export function _nwSkyMarker(sky) {
  if (!sky || sky.cloud_fraction == null) return null;
  const f = Number(sky.cloud_fraction);
  if (!Number.isFinite(f)) return null;
  const pct = Math.round(f * 100);
  const stars = sky.median_stars != null ? `, median ${Math.round(sky.median_stars)} stars per frame` : '';
  const title = `Night ${_fmtNight(sky.night)}: ${pct}% of frames clouded${stars}`;
  if (f >= 0.8) return { icon: '☁', cls: 'overcast', title };
  if (f >= 0.3) return { icon: '☁', cls: 'partly', title };
  return { icon: '✦', cls: 'clear', title };
}

function nwBuildRows(overviewData, statusAllData) {
  const rows = [];
  for (const [key, info] of Object.entries(overviewData)) {
    const status = (statusAllData || {})[key] || {};
    const services = status.services || {};
    const segmentAlarm = info.segment_alarm || {};
    const cameras = (info.cameras || []).map(c => ({
      code: c.code, svcCls: _nwCamSvcStatus(c.code, services),
      // Capture state wins over the service guess when the station reports it.
      capture: _nwCaptureDot(c.capture),
      sky: _nwSkyMarker(c.sky),
      // Per-camera cadence counters, null on stations that don't report them.
      segment: c.segment_health || null,
      segmentAlarm: segmentAlarm[c.code] || null,
    }));
    let diskPct = null;
    if (info.disk != null) {
      if (typeof info.disk === 'object') {
        if (info.disk.pct != null) diskPct = info.disk.pct;
        else if (info.disk.total_mb) diskPct = (info.disk.used_mb / info.disk.total_mb) * 100;
        else if (info.disk.total_gb) diskPct = (info.disk.used_gb / info.disk.total_gb) * 100;
      } else if (typeof info.disk === 'number') diskPct = info.disk;
    }
    const stationStatus = info.status || 'active';
    rows.push({
      key, label: info.label || '', online: info.online ? 1 : 0,
      captureRaw: (info.cameras || []).map(c => c.capture || null),
      stationStatus,
      last_seen: info.last_updated || null,
      cpu: info.cpu_pct ?? null, ram: info.ram_pct ?? null, temp: info.temp_c ?? null,
      diskPct, cameras,
      // Detection-feed weather state (from /api/overview). Both may be absent
      // on older payloads → null-safe reads below.
      detectionsStale: !!info.detections_stale,
      weather: info.weather || null,
      // Colour-segment cadence alarms, {camera code: kind}. Absent on older
      // station bundles → {} → no badge.
      segmentAlarm,
    });
  }
  return rows;
}

function _nwMarkSort() {
  document.querySelectorAll('.nw-table th').forEach(th => {
    th.classList.remove('sort-asc', 'sort-desc');
    const on = th.dataset.col === _nwSortCol;
    if (on) th.classList.add(_nwSortDir === 1 ? 'sort-asc' : 'sort-desc');
    if (th.hasAttribute('aria-sort')) {
      th.setAttribute('aria-sort', on ? (_nwSortDir === 1 ? 'ascending' : 'descending') : 'none');
    }
  });
}

function nwSort(col) {
  if (_nwSortCol === col) _nwSortDir *= -1;
  else { _nwSortCol = col; _nwSortDir = col === 'key' ? 1 : -1; }
  _nwMarkSort();
  _nwRenderTable();
}

// Sortable headers are focusable (tabindex in overview.html); Enter/Space sorts.
document.querySelector('.nw-table thead')?.addEventListener('keydown', e => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const th = e.target.closest('th[data-col][tabindex]');
  if (!th) return;
  e.preventDefault();
  nwSort(th.dataset.col);
});

function _nwSortRows(rows) {
  const col = _nwSortCol, dir = _nwSortDir;
  return [...rows].sort((a, b) => {
    let va, vb;
    switch (col) {
      case 'key':       va = a.key; vb = b.key; break;
      case 'online':    va = a.online; vb = b.online; break;
      case 'last_seen': va = a.last_seen ? new Date(a.last_seen).getTime() : 0;
                        vb = b.last_seen ? new Date(b.last_seen).getTime() : 0; break;
      case 'cpu':  va = a.cpu  ?? -1; vb = b.cpu  ?? -1; break;
      case 'ram':  va = a.ram  ?? -1; vb = b.ram  ?? -1; break;
      case 'temp': va = a.temp ?? -1; vb = b.temp ?? -1; break;
      case 'disk': va = a.diskPct ?? -1; vb = b.diskPct ?? -1; break;
      default: return 0;
    }
    if (va < vb) return -dir;
    if (va > vb) return dir;
    return a.key.localeCompare(b.key);
  });
}

function _nwRenderTable() {
  const tbody = document.getElementById('nw-tbody');
  const cardsEl = document.getElementById('nw-cards');
  if (!tbody || !_nwRows.length) return;
  const sorted = _nwSortRows(_nwRows);
  let html = '', cardHtml = '';
  for (const r of sorted) {
    const age = _nwFmtAge(r.last_seen), cpu = _nwFmtPct(r.cpu), ram = _nwFmtPct(r.ram);
    const temp = _nwFmtTemp(r.temp), disk = _nwFmtDisk(r.diskPct);
    const isComm = r.stationStatus === 'commissioning';
    const onCls = isComm ? 'comm' : (r.online ? 'up' : 'down');
    const onTxt = isComm ? 'Commissioning' : (r.online ? 'Online' : 'Offline');
    const onColor = isComm ? '#e89b3c' : (r.online ? 'var(--green)' : 'var(--red)');
    // Weather badge: a stale (quiet) but clouded station reads as ☁ overcast
    // rather than a fault. Null when there's nothing to flag.
    const wx = _nwWeatherBadge(r);
    const wxBadge = wx
      ? ` <span class="nw-weather ${wx.cls}" title="${escHtml(wx.title)}">${wx.icon}</span>`
      : '';
    // Segment-cadence badge: a camera cutting 1 s clips or stalling is losing
    // the night right now, and nothing else in this row would show it.
    const seg = _nwSegmentBadge(r);
    const segBadge = seg
      ? ` <span class="nw-seg ${seg.cls}" title="${escHtml(seg.title)}">${escHtml(seg.label)}</span>`
      : '';
    const camDots = r.cameras.map(c => {
      const segCls = c.segmentAlarm ? ' seg-alarm' : '';
      const segTip = c.segmentAlarm
        ? ` — ${c.segmentAlarm === 'stalls' ? 'capture stalling' : 'cutting short segments'}`
        : '';
      const dotCls = c.capture ? c.capture.cls : c.svcCls;
      const capTip = c.capture && c.capture.title ? ` — ${c.capture.title}` : '';
      const sky = c.sky
        ? `<span class="nw-cam-sky ${c.sky.cls}" title="${escHtml(c.sky.title)}">${c.sky.icon}</span>`
        : '';
      return `<div class="nw-cam-dot${segCls}" title="${escHtml(c.code + capTip + segTip)}"><div class="nw-cam-dot-top"><div class="nw-cam-dot-circle ${dotCls}"></div>${sky}</div><span class="nw-cam-dot-label">${escHtml(c.code)}</span></div>`;
    }).join('');
    html += `<tr class="${r.online ? '' : 'offline'}">
      <td><a class="nw-station-key" href="/station/${encodeURIComponent(r.key)}">${escHtml(r.key)}</a><div class="nw-station-label">${escHtml(r.label)}</div></td>
      <td><div class="nw-online-cell"><span class="nw-dot ${onCls}"></span><span class="nw-online-text ${onCls}">${onTxt}</span>${wxBadge}${segBadge}</div></td>
      <td><span class="nw-age ${age.cls}">${age.text}</span></td>
      <td><span class="nw-metric ${cpu.cls}">${cpu.text}</span></td>
      <td><span class="nw-metric ${ram.cls}">${ram.text}</span></td>
      <td><span class="nw-metric ${temp.cls}">${temp.text}</span></td>
      <td><span class="nw-metric ${disk.cls}">${disk.text}</span></td>
      <td><div class="nw-cam-dots">${camDots || '<span style="color:var(--muted)">—</span>'}</div></td>
    </tr>`;
    cardHtml += `<div class="nw-card ${r.online ? '' : 'offline'}">
      <div class="nw-card-top"><span class="nw-dot ${onCls}"></span><a class="nw-card-key" href="/station/${encodeURIComponent(r.key)}">${escHtml(r.key)}</a><span class="nw-card-label">${escHtml(r.label)}</span>${wxBadge}${segBadge}</div>
      <div class="nw-card-row"><span class="nw-card-field">Status: <b style="color:${onColor}">${onTxt}</b></span><span class="nw-card-field">Last contact: <b class="nw-age ${age.cls}">${age.text}</b></span></div>
      <div class="nw-card-row"><span class="nw-card-field">CPU: <b class="nw-metric ${cpu.cls}">${cpu.text}</b></span><span class="nw-card-field">RAM: <b class="nw-metric ${ram.cls}">${ram.text}</b></span><span class="nw-card-field">Temp: <b class="nw-metric ${temp.cls}">${temp.text}</b></span><span class="nw-card-field">Disk: <b class="nw-metric ${disk.cls}">${disk.text}</b></span></div>
      <div class="nw-cam-dots" style="margin-top:4px">${camDots || '<span style="color:var(--muted)">No cameras</span>'}</div>
    </div>`;
  }
  tbody.innerHTML = html;
  if (cardsEl) cardsEl.innerHTML = cardHtml;
}

function _nwRenderSummary(rows) {
  const comm = rows.filter(r => r.stationStatus === 'commissioning').length;
  const online = rows.filter(r => r.online && r.stationStatus !== 'commissioning').length;
  const total = rows.length;
  const offline = total - online - comm;
  const totalCams = rows.reduce((s, r) => s + r.cameras.length, 0);
  const el = document.getElementById('nw-summary');
  if (!el) return;
  let html = `
    <div class="nw-summary-item green"><b>${online}</b>Online</div>
    <div class="nw-summary-item red"><b>${offline}</b>Offline</div>`;
  if (comm > 0) html += `
    <div class="nw-summary-item" style="color:#e89b3c"><b>${comm}</b>Commissioning</div>`;
  html += `
    <div class="nw-summary-item blue"><b>${totalCams}</b>Cameras</div>`;
  const caps = rows.filter(r => r.stationStatus !== 'commissioning')
    .flatMap(r => r.captureRaw || []);
  const live = caps.filter(c => c && _CAPTURE_LIVE.has(c.state));
  if (live.length) {
    const rec = live.filter(c => c.state !== 'stalled').length;
    html += `
    <div class="nw-summary-item green" title="Cameras with an FF file in the last 2 minutes"><b>${rec}</b>Recording</div>`;
  } else if (caps.some(c => c && c.last_night)) {
    const full = caps.filter(c => c && c.last_night && c.last_night.verdict === 'full').length;
    html += `
    <div class="nw-summary-item green" title="Cameras whose last night's FF files cover 90% of the dark hours or more"><b>${full}</b>Full last night</div>`;
  }
  el.innerHTML = html;
}

async function nwFetch() {
  try {
    const [overview, statusAll] = await Promise.all([fetchJson('/api/overview'), fetchJson('/api/status/all')]);
    _nwRows = nwBuildRows(overview, statusAll);
    _nwRenderSummary(_nwRows);
    _nwRenderTable();
    _nwMarkSort();
    const el = document.getElementById('nw-last-fetch');
    if (el) el.textContent = _utcTimeFmt.format(new Date()) + ' UTC';
  } catch(e) {
    const tbody = document.getElementById('nw-tbody');
    if (tbody && !_nwRows.length) tbody.innerHTML = '<tr><td colspan="8" class="nw-error">Failed to load station data; retrying in 30 s.</td></tr>';
  }
}

function renderSummary(data) {
  renderOverviewSummary(data);
  // Counted from each camera's FF files (captureSummary), not from service
  // state: a unit stays "active" all day and while its camera is unplugged.
  const capSum = captureSummary(data);
  const capturingCams = capSum.recording + capSum.degraded;
  // Network overview: the live snapshots load only from this button.
  const liveBtn = document.getElementById('ov-live-bar-btn');
  const liveNote = document.getElementById('ov-live-bar-note');
  if (liveBtn) {
    liveBtn.disabled = capturingCams === 0 && !_liveVisible;
    if (liveNote) liveNote.textContent = captureHeadline(capSum);
  }
}

let _ovStationSelBuilt = false;
function ovBuildStationSelector(data) {
  if (_ovStationSelBuilt) return;
  _ovStationSelBuilt = true;
  const el = document.getElementById('ov-station-sel');
  if (!el) return;
  const hosts = Object.entries(data).sort((a,b) => a[0].localeCompare(b[0]));
  const opts = hosts.map(([k, info]) => {
    const dot = info.online ? 'dot-up' : 'dot-down';
    return `<a class="ov-station-opt" href="/station/${encodeURIComponent(k)}"><span class="pill-dot ${dot}"></span>${escHtml(k)} <span style="color:var(--muted);margin-left:auto;font-size:11px">${escHtml(info.label||'')}</span></a>`;
  }).join('');
  el.style.display = '';
  el.innerHTML = `<button class="ov-station-sel-btn" onclick="this.nextElementSibling.classList.toggle('open');event.stopPropagation()">Go to station &#9660;</button><div class="ov-station-menu" id="ov-station-menu">${opts}</div>`;
  document.addEventListener('click', () => { const m = document.getElementById('ov-station-menu'); if (m) m.classList.remove('open'); });
}

(async function(){
  try{
    const meta=await fetchJson('/api/stations');
    for(const [key,m] of Object.entries(meta)){
      if(m.lat&&m.lon) STATION_COORDS[key]={lat:m.lat,lon:m.lon};
    }
  }catch(e){}

  try{
    // Progressive render: paint summary + map as soon as /api/overview lands
    // (one round-trip, ~140 ms TTFB from Romania), THEN fan out platepars in
    // parallel and refine the map markers when each arrives. Previously this
    // awaited every platepar before showing anything, blocking first paint
    // by ~N × RTT on top of the initial fetch.
    const data=await fetchJson('/api/overview');
    // Start only the sections the page has (the home page "/" runs
    // dashboard-home.js). The live view reads mapData without a map.
    const has=id=>!!document.getElementById(id);
    const onMap=has('map'), onNetwork=has('nw-section');
    mapData=data;
    if(onMap){ initMap(data); ovBuildStationSelector(data); }
    if(has('ov-summary')) renderSummary(data);
    // Bortle doesn't block paint either: it fires in the background and
    // self-renders once data arrives.
    if(onMap) loadBortleData(data);
    if(onNetwork) nwFetch();
    // The network numbers read the server's platepar_store, which the
    // stations' pushed platepars keep current (#1248), so they load at once.
    if(has('ov-net-stats')) loadOverviewNetStats();
    // Platepars only refine the map's FOV polygons: fetched on the map page
    // alone, in parallel and non-blocking, each redrawing its polygon when it
    // arrives. Stations without a platepar just skip silently.
    const onlineKeys=Object.entries(data).filter(([,i])=>i.online).map(([k])=>k);
    let _ppRedrawScheduled=false;
    function _schedulePlateparRedraw(){
      if(_ppRedrawScheduled) return;
      _ppRedrawScheduled=true;
      requestAnimationFrame(()=>{
        _ppRedrawScheduled=false;
        try{ if(footprintLayer&&map) drawFootprints(data, FOV_ALT_KM); }catch(e){}
      });
    }
    if(onMap) Promise.allSettled(onlineKeys.map(async key=>{
      try{
        const pp=await fetchJson(`/api/platepar/${key}`);
        if(pp&&typeof pp==='object'&&!pp.error){
          PLATEPAR_DATA[key]=pp;
          _schedulePlateparRedraw();
        }
      }catch(e){}
    }));
    // Overview refresh — was 10 s, bumped to 30 s. Server cache TTL is 60 s
    // anyway and the response carries ETag/Cache-Control: max-age=30, so a
    // shorter interval just wastes round-trips. Also pause when tab hidden.
    setInterval(async()=>{
      if(document.hidden) return;
      try{ const d=await fetchJson('/api/overview'); mapData=d; if(has('ov-summary')) renderSummary(d); }catch(e){}
      if(onNetwork) try{ nwFetch(); }catch(e){}
    },30000);
    // On tab refocus, catch up the paused pollers so the user doesn't stare
    // at up-to-30-s-stale data while the next setInterval tick is pending.
    document.addEventListener('visibilitychange', ()=>{
      if(document.hidden) return;
      (async()=>{ try{ const d=await fetchJson('/api/overview'); mapData=d; if(has('ov-summary')) renderSummary(d); }catch(e){} })();
      if(onNetwork) try{ nwFetch(); }catch(e){}
    });
    // The shared startUTCClock() in dashboard-common.js paints #utc-clock with
    // the full "YYYY-MM-DD HH:MM:SS UTC" label every second; no local ticker.
  }catch(e){
    const sum=document.getElementById('ov-summary');
    if(sum) sum.innerHTML='<div class="ov-summary-loading" style="color:var(--red)">Failed to load station data; reload the page to retry.</div>';
  }
})();
const _panelParam = new URLSearchParams(location.search).get('panel');
if (_panelParam) openAppPanel(_panelParam);

// Escape closes this page's own dialogs, topmost first. A shared VideoModal
// (GMN clips) open on top keeps the key to itself.
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape' || e.defaultPrevented) return;
  if (document.querySelector('.vm-backdrop.open, .vm-backdrop[aria-hidden="false"]')) return;
  const isOpen = id => document.getElementById(id)?.classList.contains('open');
  if (isOpen('ov-live-modal')) closeLiveModal();
  else if (isOpen('app-panel')) appPanelClose(null);
  else return;
  e.preventDefault();
});

// ── Window exposure for HTML onclick/oninput handlers ────────────────────────
// Functions called from inline event handlers in the HTML template must be
// on the global (window) scope because ES module scope is not accessible
// from HTML attributes.
window.toggleAllFov = toggleAllFov;
window.setFovAlt = setFovAlt;
window.switchMapLayer = switchMapLayer;
window.toggleLayerMenu = toggleLayerMenu;
window.toggleCovLayer = toggleCovLayer;
window.toggleGmnLayer = toggleGmnLayer;
window.toggleLpLayer = toggleLpLayer;
window.openAppPanel = openAppPanel;
window.appPanelClose = appPanelClose;
window.nwSort = nwSort;
window.closeLiveModal = closeLiveModal;
window.toggleLiveView = toggleLiveView;
window.onGmnDateChange = onGmnDateChange;
window.ovLiveUpdateZoom = ovLiveUpdateZoom;
window.setMapBrightness = setMapBrightness;

// ── Cameras at a point ─────────────────────────────────────────────────────────
// "Cameras at a point" arms a one-shot pick: the next map click opens a popup
// that asks for the meteor height, lists the cameras whose footprint (the ring
// the map draws, from each camera's last platepar) contains the point at that
// height or anywhere in a height range, then takes a UTC time and opens the
// Detections footage grid with only those cameras. Plain map clicks keep
// closing the station panel, so the pick is behind the button.

let _pickMode = false;
let _pickMarker = null;

function _setPickMode(on) {
  _pickMode = !!on;
  setPressed(document.getElementById('pick-point-btn'), _pickMode);
  map?.getContainer().classList.toggle('ov-pick-mode', _pickMode);
}

function togglePointPick() {
  _setPickMode(!_pickMode);
}

// Every camera the map can draw a footprint for, in station-number order,
// with a footprint function per height. Built from the same station list and
// platepars the map uses, so a station hidden from the map (or from an
// anonymous visitor) is absent here too.
function _pickCameras() {
  const out = [];
  if (!mapData) return out;
  for (const key of Object.keys(mapData).sort()) {
    const info = mapData[key];
    const lat = info.lat ?? STATION_COORDS[key]?.lat;
    const lon = info.lon ?? STATION_COORDS[key]?.lon;
    if (!lat || !lon || info.show_on_map === false) continue;
    const pps = PLATEPAR_DATA[key] || {};
    for (const cam of (info.cameras || [])) {
      const pp = pps[cam.code];
      out.push({
        key, cam: cam.code, label: info.label || '', lat, lon,
        footprintAt: H => _cameraFootprintPts(lat, lon, cam, pp, H)?.pts || null,
      });
    }
  }
  return out;
}

function _pickCoordsHtml(ll) {
  const ns = ll.lat >= 0 ? 'N' : 'S';
  const ew = ll.lng >= 0 ? 'E' : 'W';
  return `${Math.abs(ll.lat).toFixed(3)}&deg;&nbsp;${ns}, ${Math.abs(ll.lng).toFixed(3)}&deg;&nbsp;${ew}`;
}

function _openPointPicker(ll) {
  _setPickMode(false);
  if (_pickMarker) _pickMarker.remove();
  const el = document.createElement('div');
  el.className = 'pp-pick';
  // The buttons re-render the popup, so a click's target is detached by the
  // time it bubbles to the map; Leaflet then cannot tell it came from the
  // popup (it walks the target's parents), takes it for a map click and
  // closes the popup. Stop popup clicks at this stable container.
  L.DomEvent.disableClickPropagation(el);
  L.DomEvent.on(el, 'click', L.DomEvent.stopPropagation);
  // Top padding keeps the popup clear of the FOV / altitude toolbar.
  _pickMarker = L.popup({ className: 'gmn-popup pp-popup', maxWidth: 380, minWidth: 280,
                          autoPanPaddingTopLeft: L.point(20, 70) })
    .setLatLng(ll).setContent(el).openOn(map);  _renderPickHeight(el, ll, { mode: 'range', lo: PICK_ALT_MIN_KM, hi: PICK_ALT_MAX_KM, single: 90 });
}

function _renderPickHeight(el, ll, h) {
  el.innerHTML = `
    <div class="pp-title">Cameras at this point</div>
    <div class="pp-sub">${_pickCoordsHtml(ll)}</div>
    <div class="pp-q">Meteor height?</div>
    <label class="pp-opt"><input type="radio" name="pp-mode" value="range"${h.mode === 'range' ? ' checked' : ''}>
      Range <input type="number" class="pp-num" data-f="lo" min="10" max="200" step="1" value="${h.lo}" aria-label="Lowest height (km)">
      to <input type="number" class="pp-num" data-f="hi" min="10" max="200" step="1" value="${h.hi}" aria-label="Highest height (km)"> km</label>
    <label class="pp-opt"><input type="radio" name="pp-mode" value="single"${h.mode === 'single' ? ' checked' : ''}>
      Single height <input type="number" class="pp-num" data-f="single" min="10" max="200" step="1" value="${h.single}" aria-label="Height (km)"> km</label>
    <div class="pp-actions"><button type="button" class="fov-btn active" data-act="find">Find cameras</button></div>
    <div class="pp-err" role="alert"></div>`;
  // Typing in a field selects its option.
  el.querySelectorAll('.pp-num').forEach(inp => inp.addEventListener('focus', () => {
    const radio = inp.closest('.pp-opt')?.querySelector('input[type=radio]');
    if (radio) radio.checked = true;
  }));
  el.querySelector('[data-act=find]').addEventListener('click', () => {
    const num = f => Number(el.querySelector(`[data-f=${f}]`).value);
    const mode = el.querySelector('input[name=pp-mode]:checked')?.value || 'range';
    const next = { mode, lo: num('lo'), hi: num('hi'), single: num('single') };
    const vals = mode === 'range' ? [next.lo, next.hi] : [next.single];
    if (vals.some(v => !Number.isFinite(v) || v < 10 || v > 200)) {
      el.querySelector('.pp-err').textContent = 'Heights must be between 10 and 200 km.';
      return;
    }
    _renderPickResult(el, ll, next);
  });
  _pickMarker?.update();
}

function _renderPickResult(el, ll, h) {
  const heights = h.mode === 'range' ? altitudeSamples(h.lo, h.hi) : [h.single];
  const matches = camerasSeeingPoint(ll.lat, ll.lng, heights, _pickCameras());
  const groups = groupByStation(matches);
  const span = h.mode === 'range'
    ? `${Math.min(h.lo, h.hi)}&ndash;${Math.max(h.lo, h.hi)}&nbsp;km` : `${h.single}&nbsp;km`;
  const list = groups.map(g => `
    <div class="pp-station">${escHtml(stationLabel(g.key, g.label))}</div>
    <ul class="pp-cams">${g.cams.map(m => `
      <li><span class="pp-cam" translate="no">${escHtml(m.cam)}</span>
        <span class="pp-meta">${Math.round(m.distKm)}&nbsp;km away, ${Math.round(m.elevDeg)}&deg; up${
          h.mode === 'range' ? `, seen at ${m.minH === m.maxH ? m.minH : `${m.minH}&ndash;${m.maxH}`}&nbsp;km` : ''}</span></li>`).join('')}
    </ul>`).join('');
  const now = utcDateTimeParts();
  el.innerHTML = `
    <div class="pp-title">${matches.length} camera${matches.length === 1 ? '' : 's'} see this point at ${span}</div>
    <div class="pp-sub">${_pickCoordsHtml(ll)} &middot; <a href="#" data-act="height">change height</a></div>
    <div class="pp-list">${list || '<div class="pp-none">No camera covers this point at that height.</div>'}</div>
    <div class="pp-note">Geometry only: masks, obstructions and meteor brightness are not considered. Footprints come from each camera's last platepar.</div>
    ${matches.length ? `
    <div class="pp-q">Event time (UTC)</div>
    <div class="pp-time">
      <input type="date" class="pp-date" value="${now.date}" aria-label="Date (UTC)">
      <input type="text" class="pp-hhmm" inputmode="numeric" placeholder="HH:MM" maxlength="5" value="${now.time}" aria-label="Time (UTC, HH:MM)">
      <span class="muted">UTC</span>
    </div>
    <div class="pp-actions"><button type="button" class="fov-btn active" data-act="view">View these cameras</button></div>
    <div class="pp-hint muted">Opens the footage grid with these cameras, 2&nbsp;min before to 3&nbsp;min after.</div>
    <div class="pp-err" role="alert"></div>` : ''}`;
  el.querySelector('[data-act=height]').addEventListener('click', e => {
    e.preventDefault();
    _renderPickHeight(el, ll, h);
  });
  el.querySelector('[data-act=view]')?.addEventListener('click', () => {
    const date = el.querySelector('.pp-date').value;
    const hhmm = el.querySelector('.pp-hhmm').value;
    const night = nightForUtc(date, hhmm);
    const win = gridWindowFor(hhmm);
    if (!night || !win) {
      el.querySelector('.pp-err').textContent = 'Enter a date and a UTC time as HH:MM.';
      return;
    }
    window.location.href = gridUrlFor(night, win, matches.map(m => m.cam));
  });
  _pickMarker?.update();
}

window.togglePointPick = togglePointPick;

