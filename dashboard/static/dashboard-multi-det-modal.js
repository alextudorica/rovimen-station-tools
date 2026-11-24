/**
 * MultiDetModal — synchronized multi-camera detection viewer.
 *
 * All videos are controlled globally (no per-video controls).
 * Click any video cell to focus it (big); click again to return to grid.
 * Controls: play/pause · ½× speed · ⏮ rewind · ⏮◆ seek-to-detection · track bar
 * Stitched download mirrors current layout (grid or focused).
 * Dome button: real-time WebGL2 all-sky projection from synchronized videos.
 */

// ── WebGL2 dome renderer ──────────────────────────────────────────────────────
// Inlined to avoid relative-import resolution issues with the import-map setup.
// Geometry matches celestial_dome.py: dome_xy_to_altaz → altaz_to_pixel.

const _DOME_VERT = /* glsl */`#version 300 es
in vec2 a_pos;
out vec2 v_uv;
void main() {
  v_uv = a_pos * 0.5 + 0.5;
  gl_Position = vec4(a_pos, 0.0, 1.0);
}`;

const _DOME_FRAG = /* glsl */`#version 300 es
precision highp float;
const float PI           = 3.141592653589793;
const float HORIZON_FRAC = 0.93;
const int   MAX_CAMS     = 8;
const vec3  BG           = vec3(5.0/255.0, 5.0/255.0, 16.0/255.0);
uniform int       u_n;
uniform sampler2D u_tex[8];
// u_cam_a[i] = (az_centre_deg, alt_centre_deg, F_scale, rotation_from_horiz_deg)
// u_cam_b[i] = (X_res, Y_res, fov_h_deg, fov_v_deg)
uniform vec4 u_cam_a[8];
uniform vec4 u_cam_b[8];
in  vec2 v_uv;
out vec4 fragColor;
vec2 altazToCamUV(float alt_deg, float az_deg, vec4 a, vec4 b) {
  float alt0=radians(a.y), az0=radians(a.x), pa=radians(a.w), fs=a.z;
  float alt=radians(alt_deg), az=radians(az_deg);
  float daz=az-az0;
  float cos_rho=clamp(sin(alt)*sin(alt0)+cos(alt)*cos(alt0)*cos(daz),-1.0,1.0);
  float rho=acos(cos_rho);
  float y_pa=sin(daz)*cos(alt);
  float x_pa=cos(alt0)*sin(alt)-sin(alt0)*cos(alt)*cos(daz);
  float theta=atan(y_pa,x_pa);
  float rho_d=degrees(rho);
  float d_az=rho_d*sin(theta), d_alt=rho_d*cos(theta);
  float cr=cos(pa), sr=sin(pa);
  float dx=(d_az*cr+d_alt*sr)*fs, dy=(d_az*sr-d_alt*cr)*fs;
  return vec2((b.x*0.5+dx)/b.x, 1.0-(b.y*0.5+dy)/b.y);
}
// Samplers must be accessed with a constant index on many GPU drivers.
// Use a helper with explicit if-else so the compiler sees constant indices.
vec4 sampleTex(int idx, vec2 uv) {
  if (idx==0) return texture(u_tex[0],uv);
  if (idx==1) return texture(u_tex[1],uv);
  if (idx==2) return texture(u_tex[2],uv);
  if (idx==3) return texture(u_tex[3],uv);
  if (idx==4) return texture(u_tex[4],uv);
  if (idx==5) return texture(u_tex[5],uv);
  if (idx==6) return texture(u_tex[6],uv);
  return texture(u_tex[7],uv);
}
void main() {
  vec2 dc=v_uv-0.5;
  float r=length(dc);
  if (r>=0.5) { fragColor=vec4(0.0,0.0,0.0,1.0); return; }
  float r_norm=r/0.5;
  if (r_norm>HORIZON_FRAC) { fragColor=vec4(BG,1.0); return; }
  float alt_deg=90.0*(1.0-r_norm/HORIZON_FRAC);
  float az_deg=mod(degrees(atan(dc.x,-dc.y)),360.0);
  vec3 color=vec3(0.0); float total_w=0.0;
  for (int i=0; i<MAX_CAMS; i++) {
    if (i>=u_n) break;
    vec4 a=u_cam_a[i], b=u_cam_b[i];
    vec2 uv_c=altazToCamUV(alt_deg,az_deg,a,b);
    if (any(lessThan(uv_c,vec2(0.0)))||any(greaterThan(uv_c,vec2(1.0)))) continue;
    float alt_r=radians(alt_deg), az_r=radians(az_deg);
    float daz_w=az_r-radians(a.x);
    float cr_w=clamp(sin(alt_r)*sin(radians(a.y))+cos(alt_r)*cos(radians(a.y))*cos(daz_w),-1.0,1.0);
    float rho_w=degrees(acos(cr_w));
    float diag=0.5*sqrt(b.z*b.z+b.w*b.w);
    float w=clamp(1.0-pow(rho_w/diag,2.0),0.0,1.0);
    if (w<=0.0) continue;
    color+=sampleTex(i,uv_c).rgb*w; total_w+=w;
  }
  fragColor=total_w>1e-6 ? vec4(color/total_w,1.0) : vec4(BG,1.0);
}`;

class DomeWebGL {
  constructor(canvas) {
    this._canvas=canvas; this._cameras=[]; this._textures=[]; this._n=0;
    const gl=canvas.getContext('webgl2',{antialias:false,alpha:false});
    if (!gl) throw new Error('WebGL2 not supported');
    this._gl=gl;
    this._prog=_domeCompileProgram(gl,_DOME_VERT,_DOME_FRAG);
    _domeInitQuad(gl,this._prog);
  }
  setCameras(cameras) {
    const gl=this._gl;
    for (const t of this._textures) gl.deleteTexture(t);
    this._textures=[];
    const n=Math.min(cameras.length,8);
    this._n=n; this._cameras=cameras.slice(0,n);
    for (let i=0;i<n;i++) {
      const tex=gl.createTexture();
      gl.activeTexture(gl.TEXTURE0+i);
      gl.bindTexture(gl.TEXTURE_2D,tex);
      gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);
      this._textures.push(tex);
    }
    gl.useProgram(this._prog);
    gl.uniform1i(gl.getUniformLocation(this._prog,'u_n'),n);
    for (let i=0;i<8;i++) {
      const loc=gl.getUniformLocation(this._prog,`u_tex[${i}]`);
      if (loc!=null) gl.uniform1i(loc,i);
    }
    for (let i=0;i<n;i++) {
      const pp=cameras[i].platepar||{};
      const aLoc=gl.getUniformLocation(this._prog,`u_cam_a[${i}]`);
      const bLoc=gl.getUniformLocation(this._prog,`u_cam_b[${i}]`);
      if (aLoc) gl.uniform4f(aLoc, pp.az_centre??180, pp.alt_centre??45, pp.F_scale??2, pp.rotation_from_horiz??0);
      if (bLoc) gl.uniform4f(bLoc, pp.X_res??1920, pp.Y_res??1080, pp.fov_h??60, pp.fov_v??40);
    }
  }
  render() {
    const gl=this._gl, canvas=this._canvas;
    const p=canvas.parentElement;
    if (p) { const s=Math.min(p.clientWidth,p.clientHeight); if (canvas.width!==s||canvas.height!==s){canvas.width=canvas.height=s;} }
    for (let i=0;i<this._n;i++) {
      const v=this._cameras[i]?.videoEl;
      if (!v||v.readyState<2||v.videoWidth===0) continue;
      gl.activeTexture(gl.TEXTURE0+i);
      gl.bindTexture(gl.TEXTURE_2D,this._textures[i]);
      gl.texImage2D(gl.TEXTURE_2D,0,gl.RGB,gl.RGB,gl.UNSIGNED_BYTE,v);
    }
    gl.viewport(0,0,canvas.width,canvas.height);
    gl.useProgram(this._prog);
    gl.drawArrays(gl.TRIANGLE_STRIP,0,4);
  }
  destroy() {
    const gl=this._gl;
    for (const t of this._textures) gl.deleteTexture(t);
    gl.deleteProgram(this._prog);
    this._n=0; this._cameras=[]; this._textures=[];
  }
}

function _domeCompileProgram(gl,vSrc,fSrc) {
  const vs=_domeCompileShader(gl,gl.VERTEX_SHADER,vSrc);
  const fs=_domeCompileShader(gl,gl.FRAGMENT_SHADER,fSrc);
  const p=gl.createProgram();
  gl.attachShader(p,vs); gl.attachShader(p,fs); gl.linkProgram(p);
  if (!gl.getProgramParameter(p,gl.LINK_STATUS)) throw new Error('Dome link: '+gl.getProgramInfoLog(p));
  gl.deleteShader(vs); gl.deleteShader(fs);
  return p;
}
function _domeCompileShader(gl,type,src) {
  const s=gl.createShader(type);
  gl.shaderSource(s,src); gl.compileShader(s);
  if (!gl.getShaderParameter(s,gl.COMPILE_STATUS)) throw new Error('Dome shader: '+gl.getShaderInfoLog(s));
  return s;
}
function _domeInitQuad(gl,prog) {
  const buf=gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER,buf);
  gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,1,-1,-1,1,1,1]),gl.STATIC_DRAW);
  const loc=gl.getAttribLocation(prog,'a_pos');
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc,2,gl.FLOAT,false,0,0);
}

function _domeWebgl2Supported() {
  try { return !!document.createElement('canvas').getContext('webgl2'); }
  catch(_) { return false; }
}
const _WEBGL2 = _domeWebgl2Supported();

import { canHide, toggleHide } from './dashboard-hidden.js';
import { state, fullResStackUrl, _toast, keepClip, retryVideoIfPreparing, saveDownload, saveBlob, downloadFailedText, CLIP_PRE_S, CLIP_POST_S } from './dashboard-common.js';

const _CAM_RE  = /^[A-Z0-9]{1,16}$/;
const _DATE_RE = /^\d{8}$/;
const _FILE_RE = /^[A-Za-z0-9_]+\.(mkv|mp4)$/;

/** True for an anonymous public visitor (no logged-in session). */
function _mdmIsAnon() {
  return !state.AUTH_USER;
}

/** Per-tile download: the whole clip as MP4 via /download/ (archive copy, else
 *  the station's) when the tile names its station, camera, night and .mkv;
 *  otherwise the caller's downloadUrl. */
export function mdmTileDownloadUrl(w) {
  if (w && w.station && w.cam && _DATE_RE.test(w.date || '')
      && /^[A-Za-z0-9._-]+\.mkv$/.test(w.filename || '')) {
    return `/download/${encodeURIComponent(w.station)}/${encodeURIComponent(w.cam)}` +
           `/${w.date}/${encodeURIComponent(w.filename)}`;
  }
  return (w && w.downloadUrl) || null;
}

/** Per-tile download of the selection: the archive's trimmed cut of this
 *  clip for [trimStart, trimEnd] seconds around its detection, the same
 *  window the joint clip uses (routes/archive.py api_stitch_multi), or null
 *  when the tile does not name its camera, night and clip. */
export function mdmTileTrimUrl(w, trimStart, trimEnd) {
  if (!(w && _CAM_RE.test(w.cam || '') && _DATE_RE.test(w.date || '')
        && _FILE_RE.test(w.filename || ''))) return null;
  const offset = Number(w.offset) || 0;
  const ss = Math.max(0, offset + trimStart);
  const t = Math.max(0.5, offset + trimEnd - ss);
  return `/api/archive/shortclip/${w.cam}/${w.date}/${encodeURIComponent(w.filename)}` +
         `?ss=${ss.toFixed(2)}&t=${t.toFixed(2)}`;
}

/** Tile caption: which station and camera a tile is ("Johannismühle ·
 *  DE001C"; the Video DB passes the clip time as its label). */
export function mdmTileCaption(w) {
  const parts = [w && w.label, w && w.cam].filter(Boolean);
  return [...new Set(parts)].join(' · ');
}

/** Canvas geometry for the stack-grid download: the same column count as the
 *  on-screen grid (gridColsFor), each cell a caption strip over a cellW x cellH
 *  image, `pad` px between cells and round the edge, an optional title strip. */
export function stackGridGeometry(n, cellW, cellH, capH, titleH = 0, pad = 8) {
  const cols = gridColsFor(n);
  const rows = Math.max(1, Math.ceil(n / cols));
  const cells = [];
  for (let i = 0; i < n; i++) {
    cells.push({
      x: pad + (i % cols) * (cellW + pad),
      y: titleH + pad + Math.floor(i / cols) * (capH + cellH + pad),
    });
  }
  return {
    width: pad + cols * (cellW + pad),
    height: titleH + pad + rows * (capH + cellH + pad),
    cells, pad, titleH,
  };
}

/** Grid columns for n cameras: side by side up to 2, 2x2 up to 4, then 3 wide. */
export function gridColsFor(n) {
  return n <= 2 ? n : n <= 4 ? 2 : 3;
}

/** Resolve to the loaded image, or null when there is no URL or it fails. */
function loadImage(url) {
  if (!url) return Promise.resolve(null);
  return new Promise(resolve => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => resolve(null);
    img.src = url;
  });
}

// ── Resizable grid tracks ─────────────────────────────────────────────────────
//
// The grid ships every camera the same size, which is the right default and the
// wrong one the moment two cameras caught the meteor and four caught the sky
// around it. Focus mode is all-or-nothing (one big cell, the rest in a fixed
// 184px rail), so widening one cell to 60% while the others share the rest had
// no expression. Column and row boundaries become draggable instead: each track
// keeps an `fr` weight, a drag moves weight between the two tracks either side
// of the boundary it grabbed, and everything else holds still.

// Must match the `gap` / `padding` on .mdm-grid — used to place the drag
// handles over the gutters between tracks.
const _MDM_GRID_GAP_PX = 3;
const _MDM_GRID_PAD_PX = 3;
// A track never shrinks past this share of the two it is being traded against,
// so a cell can be made small but never dragged out of existence.
const _MDM_MIN_TRACK_FR = 0.18;

/** Pixel offsets of the boundaries between tracks, given their fr weights.
 *
 *  Returned offsets are the CENTRE of each gutter, measured from the grid's
 *  padding edge — one per boundary, so ``fr.length - 1`` of them. Exported for
 *  the unit tests, which have no layout engine to measure against.
 */
export function trackBoundaries(fr, availablePx, gap = _MDM_GRID_GAP_PX, pad = _MDM_GRID_PAD_PX) {
  const total = fr.reduce((a, b) => a + b, 0);
  const content = availablePx - 2 * pad - (fr.length - 1) * gap;
  if (!(content > 0) || !(total > 0)) return [];
  const out = [];
  let cursor = pad;
  for (let i = 0; i < fr.length - 1; i++) {
    cursor += content * (fr[i] / total);
    out.push(cursor + gap / 2);
    cursor += gap;
  }
  return out;
}

/** Move ``deltaFr`` of weight from track ``idx + 1`` into track ``idx``.
 *
 *  The pair's combined weight is conserved, so dragging one boundary never
 *  disturbs the tracks beyond it. Returns a new array; the caller decides
 *  whether to commit it.
 */
export function resizeTrackPair(fr, idx, deltaFr) {
  const out = fr.slice();
  const pair = out[idx] + out[idx + 1];
  const lo = _MDM_MIN_TRACK_FR;
  const hi = pair - _MDM_MIN_TRACK_FR;
  if (hi <= lo) return out;                       // pair too small to divide
  out[idx] = Math.min(hi, Math.max(lo, out[idx] + deltaFr));
  out[idx + 1] = pair - out[idx];
  return out;
}

let _mdmCount = 0;

export class MultiDetModal {
  constructor(containerEl) {
    this._id         = ++_mdmCount;
    this._el         = containerEl;
    this._shell      = null;
    this._videos     = [];   // { el, offset, cam, date, filename, station, label }
    this._focusedIdx = -1;
    this._playing    = false;
    this._slowActive = false;
    this._winPre     = CLIP_PRE_S;
    this._winPost    = CLIP_POST_S;
    this._trimStart  = null;
    this._trimEnd    = null;
    this._raf        = null;
    this._opts       = null;
    this._total      = 0;
    this._metaDone   = 0;
    this._seekDone   = 0;
    this._stackMode  = false;
    this._domeMode   = false;
    this._domeWebgl  = null;
    this._domeCanvas = null;
    this._platepars  = null;
    // Per-track `fr` weights for the equal-size grid. Session state only: a
    // layout the viewer dragged out survives focus mode and stack mode, and is
    // dropped on close so the next event opens even again.
    this._colFr      = [];
    this._rowFr      = [];
    this._onWinResize = () => this._positionGutters();
    MultiDetModal._instances.add(this);
  }

  // ── Public API ─────────────────────────────────────────────────────────────

  open(opts) {
    this._opts      = opts;
    this._platepars = opts.platepars || null;
    this._ensureMount();
    this._reset();

    this._winPre  = opts.windowPreSec  ?? opts.windowSec ?? CLIP_PRE_S;
    this._winPost = opts.windowPostSec ?? opts.windowSec ?? CLIP_POST_S;
    this._trimStart = -this._winPre;
    this._trimEnd   =  this._winPost;
    this._trimTouched = false;
    this._q('.mdm-title').textContent = opts.title || '';
    this._setStatus('Loading videos…', true);
    this._q('.mdm-ctrl-row').style.visibility = 'hidden';

    // Hide detection button and trackbar marker when there is no detection reference.
    const hasDet = opts.showDetection ?? true;
    const detBtn = this._q('.mdm-det-btn');
    const detMrk = this._q('.mdm-trackbar-det');
    if (detBtn) detBtn.style.display = hasDet ? '' : 'none';
    if (detMrk) detMrk.style.display = hasDet ? '' : 'none';

    const videos = opts.videos || [];
    this._total    = videos.length;
    this._metaDone = 0;
    this._seekDone = 0;

    // Show stack toggle only when at least one video has a stack image.
    const hasStacks = videos.some(v => v.stackUrl);
    const stackBtn = this._q('.mdm-stack-btn');
    if (stackBtn) stackBtn.style.display = hasStacks ? '' : 'none';

    // "Download joint clip" is an operator-only export — hide it (and remember
    // the anon state so _setStackMode below doesn't re-show it). Anonymous
    // visitors keep synchronized playback + stacks/dome view, download aside.
    // It joins only the tiles that have a clip, so it is also hidden when that
    // would leave one camera out of several: that "joint clip" is just the
    // one tile, which its own download already gives.
    this._anon = _mdmIsAnon();
    const withClip = videos.filter(v => v.url && v.filename).length;
    this._canStitch = !this._anon && (withClip >= 2 || (withClip === 1 && videos.length === 1));
    const stitchInit = this._q('.mdm-stitch-btn');
    if (stitchInit) {
      stitchInit.style.display = this._canStitch ? '' : 'none';
      stitchInit.title = withClip < videos.length
        ? `One video with the ${withClip} cameras that have a clip side by side, cut to the selection`
        : 'One video with every camera side by side, cut to the selection';
    }

    // Dome only when all videos are from the same station (platepar data is per-station).
    const videoStations = new Set(videos.filter(v => v.url).map(v => v.station).filter(Boolean));
    const domeBtn = this._q('.mdm-dome-btn');
    if (domeBtn) domeBtn.style.display = videoStations.size <= 1 ? '' : 'none';

    // Reset to video mode on every open.
    this._setStackMode(false);

    const grid = this._q('.mdm-grid');
    grid.innerHTML = '';
    // Every open starts from an even grid; the drag state is per-session.
    this._colFr = [];
    this._rowFr = [];
    this._setGridCols(videos.length);

    for (let i = 0; i < videos.length; i++) {
      const w      = videos[i];
      const offset = w.offset ?? 0;
      const cell   = document.createElement('div');
      cell.className   = 'mdm-cell';
      cell.dataset.idx = String(i);
      // Keyboard: Enter / Space on a tile enlarges it (same as clicking it).
      cell.tabIndex = 0;
      cell.setAttribute('role', 'button');
      cell.setAttribute('aria-label', `Enlarge or restore ${mdmTileCaption(w)}`);
      cell.addEventListener('keydown', e => {
        if (e.target !== cell || (e.key !== 'Enter' && e.key !== ' ')) return;
        e.preventDefault();
        this._toggleFocus(i);
      });

      // Caption (station / camera) with a per-clip download for operators.
      const cap = document.createElement('div');
      cap.className = 'mdm-cap';
      const capText = document.createElement('span');
      capText.textContent = mdmTileCaption(w);
      cap.appendChild(capText);
      const tileDl = mdmTileDownloadUrl(w);
      if (tileDl && !this._anon) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'mdm-cap-dl';
        b.dataset.cam = w.cam || 'clip';
        b.title = `Download ${w.cam || 'clip'} (MP4, full resolution)`;
        b.setAttribute('aria-label', `Download ${w.cam || 'clip'}`);
        b.textContent = '\u2B07';
        b.addEventListener('click', e => { e.stopPropagation(); this._downloadTile(tileDl, w, b); });
        cap.appendChild(b);
      }
      // Hide this camera's detection as a false positive. `w.hide` is
      // {target, hidden} from the caller; the tile stays (dimmed) so the grid
      // does not reflow, and opts.onHiddenChange lets the page update its list.
      if (w.hide?.target && canHide()) {
        let hidden = !!w.hide.hidden;
        const hb = document.createElement('button');
        hb.type = 'button';
        hb.className = 'hd-btn mdm-cap-hide';
        const paint = () => {
          hb.textContent = hidden ? 'Unhide' : 'Hide';
          hb.title = hidden ? 'Show this detection in the lists again'
            : `Hide ${w.cam || 'this'} detection from the lists for everyone (a bird, satellite, plane...)`;
          cell.classList.toggle('hd-hidden', hidden);
        };
        paint();
        hb.addEventListener('click', e => {
          e.stopPropagation();
          toggleHide(w.hide.target, !hidden, resp => {
            hidden = !!resp?.hidden;
            paint();
            this._opts?.onHiddenChange?.(w, resp);
          }, { page: 'multi' });
        });
        cap.appendChild(hb);
      }
      cell.appendChild(cap);

      const video       = document.createElement('video');
      if (w.url) video.src = w.url;
      video.preload     = 'auto';
      video.controls    = false;
      video.playsInline = true;
      video.muted       = true;
      video.addEventListener('click', e => { e.stopPropagation(); this._toggleFocus(i); });
      const markUnavailable = (text = 'Unavailable', title = '') => {
        cell.classList.add('mdm-cell-error');
        const d = document.createElement('div');
        d.className   = 'mdm-err-msg';
        d.textContent = text;
        if (title) d.title = title;
        cell.appendChild(d);
        this._onMeta(video);
      };
      // A tile whose clip is still being fetched or remuxed (202/503 +
      // Retry-After) retries instead of reading "Unavailable" for good.
      video.onerror = () => {
        if (!retryVideoIfPreparing(video, { onGiveUp: markUnavailable })) markUnavailable();
      };

      // Keep every clip that plays here (Storage Box keep + station lock).
      if (w.url) {
        video.addEventListener('play', () => {
          keepClip(w.host || w.station, w.cam, w.date, w.filename);
        });
      }

      this._videos.push({
        el: video, offset,
        cam: w.cam, date: w.date, filename: w.filename,
        station: w.station, label: w.label,
        downloadUrl: w.downloadUrl,
        stackUrl: w.stackUrl || null,
        hasClip: !!w.url,
      });

      // Stack image — hidden until stack mode is activated.
      const img = document.createElement('img');
      img.className = 'mdm-stack-img';
      if (w.stackUrl) {
        img.src = w.stackUrl;
        img.addEventListener('click', e => { e.stopPropagation(); this._toggleFocus(i); });
      } else {
        img.classList.add('mdm-stack-img-missing');
      }

      cell.appendChild(video);
      cell.appendChild(img);
      grid.appendChild(cell);

      if (!w.url) {
        // A witness with no clip in the archive: its stack and why, in place
        // of a player; it counts as loaded so the others still start.
        cell.classList.add('mdm-cell-noclip');
        const why = w.unavailable || {};
        markUnavailable(why.label || 'No clip', why.title || '');
      } else if (video.readyState >= 1) this._onMeta(video);
      else video.addEventListener('loadedmetadata', () => this._onMeta(video), { once: true });
    }

    this._updateTrackBar();
    this._modalOpen();
    // The shell was display:none until _modalOpen, so the grid had no measurable
    // size to place the resize handles against — position them now that it does,
    // and keep them on their boundaries as the window changes shape.
    this._positionGutters();
    window.addEventListener('resize', this._onWinResize);
    this._startRaf();
  }

  close() {
    this._stopRaf();
    this._globalPause();
    window.removeEventListener('resize', this._onWinResize);
    if (this._domeWebgl) { this._domeWebgl.destroy(); this._domeWebgl = null; }
    this._domeMode = false;
    for (const v of this._videos) { try { v.el.pause(); v.el.src = ''; } catch (_) {} }
    this._videos     = [];
    this._focusedIdx = -1;
    this._colFr      = [];
    this._rowFr      = [];
    this._modalClose();
  }

  // ── Loading coordination ───────────────────────────────────────────────────

  _onMeta(v) {
    // Each video element may advance the barrier at most once — an element can
    // fire both `error` and `loadedmetadata`, which would otherwise double-count
    // and fire Ready/autoplay before other clips finish loading.
    if (v) {
      if (v._metaCounted) return;
      v._metaCounted = true;
    }
    this._metaDone++;
    if (this._metaDone < this._total) return;
    const startT = -this._winPre;
    let anyQueued = false;
    for (const v of this._videos) {
      if (!isFinite(v.el.duration) || v.el.duration === 0) { this._onSeeked(); continue; }
      anyQueued = true;
      v.el.addEventListener('seeked', () => this._onSeeked(), { once: true });
      v.el.currentTime = Math.max(0, v.offset + startT);
    }
    if (!anyQueued) this._onSeeked();
  }

  _onSeeked() {
    this._seekDone++;
    this._setStatus(`Buffering… ${this._seekDone}/${this._total}`, true);
    if (this._seekDone < this._total) return;
    this._setStatus('', false);
    this._q('.mdm-ctrl-row').style.visibility = '';
    // Attempt autoplay; if the browser blocks it, fall back to paused state
    this._playing = true;
    const pp = this._q('.mdm-pp-btn');
    if (pp) pp.textContent = '⏸';
    const plays = this._videos
      .filter(v => isFinite(v.el.duration) && v.el.duration > 0)
      .map(v => v.el.play().catch(() => 'blocked'));
    if (plays.length === 0) { this._globalPause(); return; }
    Promise.all(plays).then(results => {
      if (results.every(r => r === 'blocked')) this._globalPause();
    });
  }

  // ── Shell ──────────────────────────────────────────────────────────────────

  _ensureMount() {
    if (this._shell) return;
    const div = document.createElement('div');
    div.innerHTML = `
<div class="mdm-backdrop" aria-hidden="true" style="display:none" role="dialog" aria-modal="true"
     aria-labelledby="mdm-title-${this._id}" tabindex="-1">
  <div class="mdm-inner">
    <div class="mdm-header">
      <span class="mdm-title" id="mdm-title-${this._id}"></span>
      <div class="mdm-header-actions">
        <button type="button" class="mdm-stack-btn" title="Switch between stacks and videos" style="display:none">🖼 Stacks</button>
        <button type="button" class="mdm-dome-btn"  title="All-sky dome view" style="display:none">🌍 Dome</button>
        <button type="button" class="mdm-stitch-btn" title="One video with every camera side by side, cut to the selection">⬇ Download joint clip</button>
        <button type="button" class="mdm-stackdl-btn" title="One image with every camera's stack, in this grid, at full resolution" style="display:none">⬇ Download stacks</button>
        <button type="button" class="mdm-close-btn" aria-label="Close">✕</button>
      </div>
    </div>
    <div class="mdm-video-area">
      <div class="mdm-status" role="status" style="display:none"></div>
      <div class="mdm-grid"></div>
      <canvas class="mdm-dome-canvas" style="display:none"></canvas>
    </div>
    <div class="mdm-ctrl-row" style="visibility:hidden">
      <button type="button" class="mdm-btn mdm-pp-btn" title="Play / Pause" aria-label="Play or pause">⏸</button>
      <button type="button" class="mdm-btn mdm-slow-btn" title="Half speed" aria-label="Half speed" aria-pressed="false">½×</button>
      <button type="button" class="mdm-btn mdm-rewind-btn" title="Rewind to start" aria-label="Rewind to start">⏮</button>
      <button type="button" class="mdm-btn mdm-det-btn" title="Jump to detection" aria-label="Jump to detection">⏮◆</button>
      <div class="mdm-trackbar-wrap">
        <div class="mdm-trackbar" tabindex="0" role="slider" aria-label="Seek, seconds from the detection"
             aria-valuemin="-5" aria-valuemax="5" aria-valuenow="-5">
          <div class="mdm-trackbar-fill"></div>
          <div class="mdm-trackbar-det"></div>
          <div class="mdm-trackbar-ph"></div>
          <div class="mdm-trackbar-trim-start" tabindex="0" role="slider" aria-label="Selection start"></div>
          <div class="mdm-trackbar-trim-end" tabindex="0" role="slider" aria-label="Selection end"></div>
        </div>
      </div>
      <span class="mdm-time-lbl"></span>
    </div>
  </div>
</div>`;
    this._shell = div.firstElementChild;
    this._el.appendChild(this._shell);
    this._bindShell();
  }

  _bindShell() {
    this._q('.mdm-close-btn').addEventListener('click', () => this.close());
    // this._shell IS the backdrop — use it directly (querySelector won't find itself)
    this._shell.addEventListener('click', e => {
      if (e.target === this._shell) this.close();
    });
    document.addEventListener('keydown', e => {
      // A VideoModal opened on top handles (and prevents) its own Escape.
      if (e.defaultPrevented) return;
      if (this._shell?.getAttribute('aria-hidden') === 'false' && e.key === 'Escape') {
        e.preventDefault();
        this.close();
      }
    });
    // Keep Tab inside the dialog while it is open.
    this._shell.addEventListener('keydown', e => {
      if (e.key !== 'Tab') return;
      const f = [...this._shell.querySelectorAll('button:not([disabled]), a[href], input:not([disabled]), [tabindex="0"]')]
        .filter(n => n.offsetParent !== null && getComputedStyle(n).visibility !== 'hidden');
      if (!f.length) { e.preventDefault(); return; }
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && (document.activeElement === first || document.activeElement === this._shell)) {
        e.preventDefault(); last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault(); first.focus();
      }
    });
    this._q('.mdm-pp-btn').addEventListener('click',     () => this._globalTogglePlay());
    this._q('.mdm-slow-btn').addEventListener('click',   () => this._globalToggleSlow());
    this._q('.mdm-rewind-btn').addEventListener('click', () => this._globalRewind());
    this._q('.mdm-det-btn').addEventListener('click',    () => this._globalSeekToDet());
    this._q('.mdm-stack-btn').addEventListener('click',  () => this._toggleStackMode());
    this._q('.mdm-dome-btn').addEventListener('click',   () => this._toggleDomeMode());
    this._q('.mdm-stitch-btn').addEventListener('click', () => this._stitchDownload());
    this._q('.mdm-stackdl-btn').addEventListener('click', () => this._downloadStackGrid());

    const bar = this._q('.mdm-trackbar');
    const seek = e => {
      const { left, width } = bar.getBoundingClientRect();
      if (!width) return;
      const frac = Math.max(0, Math.min(1, (e.clientX - left) / width));
      this._globalSeekToT(-this._winPre + frac * (this._winPre + this._winPost));
    };
    let _trackActive = false;
    bar.addEventListener('pointerdown', e => {
      if (e.target !== bar && e.target !== this._q('.mdm-trackbar-fill')) return;
      e.preventDefault();
      _trackActive = true;
      bar.setPointerCapture(e.pointerId);
      seek(e);
    });
    bar.addEventListener('pointermove', e => { if (_trackActive) seek(e); });
    // Keyboard seek: arrows 0.5 s (Shift: 2 s), Home/End to the selection.
    bar.addEventListener('keydown', e => {
      if (e.target !== bar) return;
      const step = e.shiftKey ? 2 : 0.5;
      let t = null;
      if (e.key === 'ArrowRight' || e.key === 'ArrowUp') t = this._masterT() + step;
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') t = this._masterT() - step;
      else if (e.key === 'Home') t = this._trimStart ?? -this._winPre;
      else if (e.key === 'End') t = this._trimEnd ?? this._winPost;
      if (t === null) return;
      e.preventDefault(); e.stopPropagation();
      this._globalSeekToT(Math.max(-this._winPre, Math.min(this._winPost, t)));
    });
    bar.addEventListener('pointerup',     () => { _trackActive = false; });
    bar.addEventListener('pointercancel', () => { _trackActive = false; });

    // Trim handle drags
    for (const [sel, which] of [['.mdm-trackbar-trim-start', 'start'], ['.mdm-trackbar-trim-end', 'end']]) {
      const handle = this._q(sel);
      if (!handle) continue;
      handle.addEventListener('pointerdown', e => {
        e.preventDefault(); e.stopPropagation();
        handle.setPointerCapture(e.pointerId);
        handle.onpointermove = ev => {
          const { left, width } = bar.getBoundingClientRect();
          if (!width) return;
          const totalWin = this._winPre + this._winPost;
          const t = -this._winPre + Math.max(0, Math.min(1, (ev.clientX - left) / width)) * totalWin;
          if (which === 'start') this._trimStart = Math.min(t, this._trimEnd - 0.5);
          else                    this._trimEnd   = Math.max(t, this._trimStart + 0.5);
          this._trimTouched = true;
          this._updateTrackBar();
          this._syncTileDownloadTitles();
        };
        handle.onpointerup = () => { handle.onpointermove = null; handle.onpointerup = null; };
      });
      // Keyboard: arrows move the handle 0.1 s (Shift: 1 s).
      handle.addEventListener('keydown', e => {
        const step = e.shiftKey ? 1 : 0.1;
        let d = 0;
        if (e.key === 'ArrowRight' || e.key === 'ArrowUp') d = step;
        else if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') d = -step;
        else return;
        e.preventDefault(); e.stopPropagation();
        const s0 = this._trimStart ?? -this._winPre;
        const e0 = this._trimEnd   ??  this._winPost;
        if (which === 'start') { this._trimStart = Math.max(-this._winPre, Math.min(s0 + d, e0 - 0.5)); this._trimEnd = e0; }
        else                   { this._trimEnd   = Math.min(this._winPost, Math.max(e0 + d, s0 + 0.5)); this._trimStart = s0; }
        this._trimTouched = true;
        this._updateTrackBar();
        this._syncTileDownloadTitles();
      });
    }
  }

  _reset() {
    for (const v of this._videos) { try { v.el.pause(); v.el.src = ''; } catch (_) {} }
    this._videos = []; this._focusedIdx = -1;
    this._playing = false; this._slowActive = false;
    this._trimStart = null; this._trimEnd = null;
    this._metaDone = 0; this._seekDone = 0;
    const grid = this._q('.mdm-grid');
    if (grid) {
      grid.innerHTML = '';
      grid.classList.remove('mdm-focused', 'mdm-stack-mode');
      grid.style.gridTemplateRows = '';
    }
    this._q('.mdm-slow-btn')?.classList.remove('mdm-btn-active');
    this._q('.mdm-slow-btn')?.setAttribute('aria-pressed', 'false');
    const pp = this._q('.mdm-pp-btn');
    if (pp) pp.textContent = '⏸';
    const ctrl = this._q('.mdm-ctrl-row');
    if (ctrl) ctrl.style.visibility = '';
    const stitch = this._q('.mdm-stitch-btn');
    if (stitch) stitch.style.display = this._canStitch ? '' : 'none';
    const btn = this._q('.mdm-stack-btn');
    if (btn) btn.textContent = '🖼 Stacks';
    this._stackMode = false;
    this._setDomeMode(false);
  }

  _toggleStackMode() { this._setStackMode(!this._stackMode); }

  _setStackMode(on) {
    this._stackMode = on;
    const grid    = this._q('.mdm-grid');
    const ctrl    = this._q('.mdm-ctrl-row');
    const btn     = this._q('.mdm-stack-btn');
    const stitch  = this._q('.mdm-stitch-btn');
    const stackDl = this._q('.mdm-stackdl-btn');
    if (grid)   grid.classList.toggle('mdm-stack-mode', on);
    if (ctrl)   ctrl.style.visibility = on ? 'hidden' : '';
    if (stitch) stitch.style.display  = (on || !this._canStitch) ? 'none' : '';
    if (stackDl) stackDl.style.display = on ? '' : 'none';
    if (btn)    btn.textContent       = on ? '▶ Videos' : '🖼 Stacks';
    if (on) {
      this._globalPause();
      this._showFullResStacks();
    }
  }

  /** Stack mode shows the stacks at cell size, so swap each 480 px /stack/
   *  thumbnail for its full-res twin once that has loaded (thumbnail stays up
   *  meanwhile; a load that lands after a re-open is dropped). */
  _showFullResStacks() {
    const cells = this._q('.mdm-grid')?.querySelectorAll('.mdm-cell') || [];
    for (const cell of cells) {
      const v = this._videos[Number(cell.dataset.idx)];
      const img = cell.querySelector('.mdm-stack-img');
      const want = fullResStackUrl(v?.stackUrl);
      if (!img || !want || want === v.stackUrl || img.dataset.full === want) continue;
      const pre = new Image();
      pre.onload = () => {
        if (!img.isConnected) return;
        img.src = want;
        img.dataset.full = want;
      };
      pre.src = want;
    }
  }

  async _toggleDomeMode() {
    if (this._domeMode) { this._setDomeMode(false); return; }

    // Fetch platepars lazily on first activation (cached in this._platepars).
    if (!this._platepars && this._videos.length) {
      const host = (this._videos.find(v => v.hasClip) || this._videos[0]).station;
      const btn  = this._q('.mdm-dome-btn');
      if (btn) { btn.disabled = true; btn.textContent = '⏳'; }
      try {
        const r = await fetch(`/api/platepar/${host}`);
        if (r.ok) this._platepars = await r.json();
      } catch (_) { /* non-fatal */ }
      if (btn) { btn.disabled = false; btn.textContent = '🌍 Dome'; }
    }

    this._setDomeMode(true);
  }

  _setDomeMode(on) {
    this._domeMode = on;
    const grid    = this._q('.mdm-grid');
    const canvas  = this._q('.mdm-dome-canvas');
    const btn     = this._q('.mdm-dome-btn');

    if (!on) {
      if (canvas) canvas.style.display = 'none';
      if (grid)   grid.style.display   = '';
      if (btn)    btn.classList.remove('mdm-btn-active');
      if (this._domeWebgl) { this._domeWebgl.destroy(); this._domeWebgl = null; }
      return;
    }

    // Turning dome on: exit stack mode first
    if (this._stackMode) this._setStackMode(false);

    if (grid)   grid.style.display   = 'none';
    if (canvas) canvas.style.display = '';
    if (btn)    btn.classList.add('mdm-btn-active');

    // Build camera list: one entry per video, paired with its platepar
    const cameras = this._videos.filter(v => v.hasClip).map(v => ({
      videoEl:  v.el,
      platepar: this._platepars?.[v.cam] ?? null,
    })).filter(c => c.platepar && c.platepar.az_centre != null);

    if (!cameras.length) {
      this._setDomeMode(false);
      (window._toast || console.warn).call(null, 'No calibration data for these cameras');
      return;
    }

    try {
      if (!this._domeWebgl) this._domeWebgl = new DomeWebGL(canvas);
      this._domeWebgl.setCameras(cameras);
      this._domeWebgl.render();
    } catch (e) {
      console.error('DomeWebGL init failed:', e);
      this._setDomeMode(false);
      const msg = e.message?.includes('WebGL2') ? 'WebGL2 not available — try disabling Shields or hardware acceleration restrictions' : 'Dome render failed: ' + e.message;
      (window._toast || alert).call(null, msg);
    }
  }

  _q(sel) { return this._shell?.querySelector(sel); }

  _setStatus(msg, show) {
    const s = this._q('.mdm-status');
    if (!s) return;
    s.textContent   = msg;
    s.style.display = show ? '' : 'none';
  }

  _modalOpen() {
    const wasOpen = this._shell.getAttribute('aria-hidden') === 'false';
    if (!wasOpen) this._opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    this._shell.setAttribute('aria-hidden', 'false');
    this._shell.classList.add('open');
    this._shell.style.display = 'flex';
    document.body.style.overflow = 'hidden';
    if (!wasOpen) (this._q('.mdm-close-btn') || this._shell).focus({ preventScroll: true });
  }

  _modalClose() {
    if (!this._shell) return;
    this._shell.setAttribute('aria-hidden', 'true');
    this._shell.classList.remove('open');
    this._shell.style.display = 'none';
    document.body.style.overflow = '';
    const opener = this._opener;
    this._opener = null;
    if (opener && opener.isConnected && typeof opener.focus === 'function') opener.focus({ preventScroll: true });
  }

  // ── Grid columns ───────────────────────────────────────────────────────────

  _setGridCols(n) {
    const grid = this._q('.mdm-grid');
    if (!grid) return;
    const cols = gridColsFor(n);
    const rows = Math.max(1, Math.ceil(n / cols));
    // Only re-seed even weights when the track count actually changed —
    // leaving focus mode must restore the sizes the viewer dragged, not
    // reset them.
    if (this._colFr.length !== cols) this._colFr = Array(cols).fill(1);
    if (this._rowFr.length !== rows) this._rowFr = Array(rows).fill(1);
    this._applyTracks();
    this._buildGutters();
  }

  /** Write the current fr weights onto the grid. */
  _applyTracks() {
    const grid = this._q('.mdm-grid');
    if (!grid) return;
    grid.style.gridTemplateColumns = this._colFr.map(f => `${f}fr`).join(' ');
    grid.style.gridTemplateRows    = this._rowFr.map(f => `${f}fr`).join(' ');
  }

  // ── Cell resizing ──────────────────────────────────────────────────────────

  /** Create one drag handle per interior track boundary.
   *
   *  Handles are absolutely positioned children of the grid, so they sit over
   *  the gutters without taking part in grid flow, and they disappear with the
   *  grid when dome mode hides it.
   */
  _buildGutters() {
    const grid = this._q('.mdm-grid');
    if (!grid) return;
    grid.querySelectorAll('.mdm-gutter').forEach(el => el.remove());
    // Focus mode drives its own template; resizing is a grid-layout affordance.
    if (this._focusedIdx >= 0) return;

    const add = (axis, idx) => {
      const g = document.createElement('div');
      g.className = `mdm-gutter mdm-gutter-${axis}`;
      g.dataset.axis = axis;
      g.dataset.idx  = String(idx);
      g.title = 'Drag to resize · double-click to even out';
      g.addEventListener('pointerdown', e => this._onGutterDown(e, axis, idx));
      g.addEventListener('dblclick',    e => this._onGutterReset(e, axis, idx));
      grid.appendChild(g);
    };
    for (let i = 0; i < this._colFr.length - 1; i++) add('col', i);
    for (let i = 0; i < this._rowFr.length - 1; i++) add('row', i);
    this._positionGutters();
  }

  _positionGutters() {
    const grid = this._q('.mdm-grid');
    if (!grid) return;
    const colAt = trackBoundaries(this._colFr, grid.clientWidth);
    const rowAt = trackBoundaries(this._rowFr, grid.clientHeight);
    grid.querySelectorAll('.mdm-gutter').forEach(g => {
      const idx = +g.dataset.idx;
      if (g.dataset.axis === 'col') {
        const x = colAt[idx];
        g.style.display = x == null ? 'none' : '';
        if (x != null) g.style.left = `${x}px`;
      } else {
        const y = rowAt[idx];
        g.style.display = y == null ? 'none' : '';
        if (y != null) g.style.top = `${y}px`;
      }
    });
  }

  _onGutterDown(e, axis, idx) {
    const grid = this._q('.mdm-grid');
    if (!grid) return;
    // The cells underneath toggle focus on click — a drag must not also focus.
    e.preventDefault();
    e.stopPropagation();

    const fr    = axis === 'col' ? this._colFr : this._rowFr;
    const total = fr.reduce((a, b) => a + b, 0);
    const span  = axis === 'col' ? grid.clientWidth : grid.clientHeight;
    const content = span - 2 * _MDM_GRID_PAD_PX - (fr.length - 1) * _MDM_GRID_GAP_PX;
    if (!(content > 0)) return;
    const frPerPx = total / content;

    const start   = axis === 'col' ? e.clientX : e.clientY;
    const startFr = fr.slice();
    const target  = e.currentTarget;
    try { target.setPointerCapture(e.pointerId); } catch (_) { /* jsdom */ }
    target.classList.add('mdm-gutter-active');

    const move = ev => {
      const delta = ((axis === 'col' ? ev.clientX : ev.clientY) - start) * frPerPx;
      const next  = resizeTrackPair(startFr, idx, delta);
      if (axis === 'col') this._colFr = next; else this._rowFr = next;
      this._applyTracks();
      this._positionGutters();
    };
    const up = () => {
      target.classList.remove('mdm-gutter-active');
      target.removeEventListener('pointermove', move);
      target.removeEventListener('pointerup', up);
      target.removeEventListener('pointercancel', up);
    };
    target.addEventListener('pointermove', move);
    target.addEventListener('pointerup', up);
    target.addEventListener('pointercancel', up);
  }

  /** Double-click a boundary: split its two tracks evenly again. */
  _onGutterReset(e, axis, idx) {
    e.preventDefault();
    e.stopPropagation();
    const fr = axis === 'col' ? this._colFr : this._rowFr;
    const half = (fr[idx] + fr[idx + 1]) / 2;
    fr[idx] = half;
    fr[idx + 1] = half;
    this._applyTracks();
    this._positionGutters();
  }

  // ── Focus / Unfocus ────────────────────────────────────────────────────────
  // Cells stay in .mdm-grid at all times — CSS classes drive layout changes.
  // This avoids moving <video> elements (which resets them to black in Firefox/Chrome).

  _toggleFocus(idx) {
    if (this._focusedIdx === idx) this._unfocus();
    else this._focusVideo(idx);
  }

  _focusVideo(idx) {
    this._focusedIdx = idx;
    const grid = this._q('.mdm-grid');
    const n = this._videos.length;
    const sideCount = n - 1;

    // Mark which cell is focused
    grid.querySelectorAll('.mdm-cell').forEach(c => {
      c.classList.toggle('mdm-cell--focused', +c.dataset.idx === idx);
    });

    // Switch grid to focused layout: left column wide, right sidebar
    grid.classList.add('mdm-focused');
    grid.style.gridTemplateColumns = '1fr 184px';
    // 1fr rows so focused cell spans full container height; sidebar cells share proportionally
    grid.style.gridTemplateRows = `repeat(${sideCount || 1}, 1fr)`;
    // Focused layout owns the template — its boundaries are not the grid's.
    this._buildGutters();
  }

  _unfocus() {
    this._focusedIdx = -1;
    const grid = this._q('.mdm-grid');
    grid.querySelectorAll('.mdm-cell').forEach(c => c.classList.remove('mdm-cell--focused'));
    grid.classList.remove('mdm-focused');
    // _setGridCols re-applies the fr weights, which are unchanged, so the
    // viewer's dragged-out layout comes back rather than resetting to equal.
    this._setGridCols(this._videos.length);
  }

  // ── Global playback ────────────────────────────────────────────────────────

  _globalPlay() {
    this._playing = true;
    const pp = this._q('.mdm-pp-btn');
    if (pp) pp.textContent = '⏸';
    for (const v of this._videos) v.el.play().catch(() => {});
  }

  _globalPause() {
    this._playing = false;
    const pp = this._q('.mdm-pp-btn');
    if (pp) pp.textContent = '▶';
    for (const v of this._videos) v.el.pause();
  }

  _globalTogglePlay() {
    if (this._playing) this._globalPause();
    else this._globalPlay();
  }

  _globalToggleSlow() {
    this._slowActive = !this._slowActive;
    const rate = this._slowActive ? 0.5 : 1.0;
    for (const v of this._videos) v.el.playbackRate = rate;
    this._q('.mdm-slow-btn')?.classList.toggle('mdm-btn-active', this._slowActive);
    this._q('.mdm-slow-btn')?.setAttribute('aria-pressed', this._slowActive ? 'true' : 'false');
  }

  _globalRewind() {
    this._globalSeekToT(this._trimStart ?? -this._winPre);
    this._globalPlay();
  }

  _globalSeekToDet() {
    // 1 second before detection so user sees the approach
    this._globalSeekToT(-1);
    this._globalPlay();
  }

  _globalSeekToT(masterT) {
    for (const v of this._videos) {
      const t = Math.max(0, v.offset + masterT);
      if (isFinite(t)) v.el.currentTime = t;
    }
    this._updateTrackBar();
  }

  // ── RAF / track bar ────────────────────────────────────────────────────────

  _startRaf() {
    if (this._raf) return;
    const tick = () => { this._rafTick(); this._raf = requestAnimationFrame(tick); };
    this._raf = requestAnimationFrame(tick);
  }

  _stopRaf() {
    if (this._raf) { cancelAnimationFrame(this._raf); this._raf = null; }
  }

  _rafTick() {
    this._updateTrackBar();
    if (this._domeMode && this._domeWebgl) this._domeWebgl.render();
    if (this._playing && this._videos.length > 0) {
      const stopT   = (this._trimEnd ?? this._winPost) + 0.15;
      const allDone = this._videos.every(v => v.el.paused || v.el.ended
        || (v.el.currentTime - v.offset) >= stopT);
      if (allDone) this._globalPause();
    }
  }

  _masterT() {
    for (const v of this._videos) {
      if (isFinite(v.el.currentTime) && v.el.duration > 0)
        return v.el.currentTime - v.offset;
    }
    return -this._winPre;
  }

  _updateTrackBar() {
    const mt       = this._masterT();
    const totalWin = this._winPre + this._winPost;
    const frac     = Math.max(0, Math.min(1, (mt + this._winPre) / totalWin));

    const ph = this._q('.mdm-trackbar-ph');
    if (ph) ph.style.left = (frac * 100).toFixed(2) + '%';

    const trimS = this._trimStart ?? -this._winPre;
    const trimE = this._trimEnd   ??  this._winPost;
    const sFrac = Math.max(0, Math.min(1, (trimS + this._winPre) / totalWin));
    const eFrac = Math.max(0, Math.min(1, (trimE + this._winPre) / totalWin));

    const fill = this._q('.mdm-trackbar-fill');
    if (fill) { fill.style.left = (sFrac * 100).toFixed(2) + '%'; fill.style.width = ((eFrac - sFrac) * 100).toFixed(2) + '%'; }

    const tsh = this._q('.mdm-trackbar-trim-start');
    const teh = this._q('.mdm-trackbar-trim-end');
    if (tsh) tsh.style.left = (sFrac * 100).toFixed(2) + '%';
    if (teh) teh.style.left = (eFrac * 100).toFixed(2) + '%';

    const det = this._q('.mdm-trackbar-det');
    if (det) det.style.left = ((this._winPre / totalWin) * 100).toFixed(2) + '%';

    const lbl = this._q('.mdm-time-lbl');
    if (lbl) lbl.textContent = (mt >= 0 ? '+' : '') + mt.toFixed(1) + 's';

    const bar = this._q('.mdm-trackbar');
    if (bar) {
      bar.setAttribute('aria-valuemin', (-this._winPre).toFixed(1));
      bar.setAttribute('aria-valuemax', this._winPost.toFixed(1));
      bar.setAttribute('aria-valuenow', mt.toFixed(1));
      bar.setAttribute('aria-valuetext', `${(mt >= 0 ? '+' : '') + mt.toFixed(1)} s from the detection`);
    }
    for (const [h, v] of [[tsh, trimS], [teh, trimE]]) {
      if (!h) continue;
      h.setAttribute('aria-valuemin', (-this._winPre).toFixed(1));
      h.setAttribute('aria-valuemax', this._winPost.toFixed(1));
      h.setAttribute('aria-valuenow', v.toFixed(1));
    }
  }

  // ── Per-clip download ──────────────────────────────────────────────────────

  /** One camera's clip: the selection once the viewer has moved the trim
   *  handles (it used to be the whole 20 s chunk regardless), else the whole
   *  clip. A selection not in the archive is cut on the station instead. */
  async _downloadTile(url, w, btn) {
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = '\u2026';
    try {
      const trimmed = this._trimTouched
        ? mdmTileTrimUrl(w, this._trimStart, this._trimEnd) : null;
      let resp = await saveDownload(trimmed || url, w.filename || 'clip');
      if (trimmed && resp.status === 404 && w.station) {
        resp = await saveDownload(
          `/shortclip/${encodeURIComponent(w.station)}` + trimmed.slice('/api/archive/shortclip'.length),
          w.filename || 'clip');
      }
      if (resp.status !== 200) _toast(downloadFailedText(resp.status), 'error');
    } catch (_) {
      _toast('Download failed', 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  /** Tile download tooltips follow the selection. */
  _syncTileDownloadTitles() {
    const secs = Math.max(0, this._trimEnd - this._trimStart).toFixed(1);
    for (const b of this._shell?.querySelectorAll('.mdm-cap-dl') || []) {
      b.title = this._trimTouched
        ? `Download ${b.dataset.cam}: the selection (${secs} s)`
        : `Download ${b.dataset.cam} (MP4, full resolution)`;
    }
  }

  // ── Stitched download ──────────────────────────────────────────────────────

  async _stitchDownload() {
    // Only tiles with an archived clip can be joined; a focused tile is
    // re-indexed into that list (a focused tile without a clip joins as grid).
    const withClip   = this._videos.filter(v => v.hasClip && v.filename);
    const focusedIdx = this._focusedIdx >= 0 ? withClip.indexOf(this._videos[this._focusedIdx]) : -1;
    const btn     = this._q('.mdm-stitch-btn');
    const origTxt = btn.textContent;
    btn.disabled  = true;
    btn.textContent = '⏳ Composing…';
    try {
      const payload = {
        videos: withClip.map(v => ({
          cam: v.cam, date: v.date, filename: v.filename, offset: v.offset,
        })),
        layout:       focusedIdx >= 0 ? 'focused' : 'grid',
        focused_idx:  focusedIdx,
        master_start: this._trimStart ?? -this._winPre,
        master_end:   this._trimEnd   ??  this._winPost,
      };
      const resp = await fetch('/api/stitch-multi', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const j = await resp.json().catch(() => ({}));
      if (resp.status !== 202 || !j.url) throw new Error(j.error || `HTTP ${resp.status}`);
      // The composite renders on the server; fetch it once it is ready.
      const slug = (this._opts?.title || '').replace(/[^a-z0-9]/gi, '_').slice(0, 24);
      const name = `rovimen_multi_${slug || 'event'}`;
      const got = await saveDownload(`${j.url}?name=${encodeURIComponent(name)}`, `${name}.mp4`);
      if (got.status !== 200) {
        const e = await got.json().catch(() => ({}));
        throw new Error(e.error || downloadFailedText(got.status));
      }
    } catch (e) {
      alert('Stitch failed: ' + e.message);
    } finally {
      btn.disabled    = false;
      btn.textContent = origTxt;
    }
  }

  // ── Stack grid download ────────────────────────────────────────────────────

  /** The stacks as the grid shows them, one JPEG: drawn client-side from the
   *  full-res stacks (same origin, so the canvas stays exportable), no server
   *  render. Every camera keeps its place; one without a stack reads "No stack". */
  async _downloadStackGrid() {
    const btn = this._q('.mdm-stackdl-btn');
    const origTxt = btn.textContent;
    btn.disabled = true;
    btn.textContent = '⏳ Composing…';
    try {
      const imgs = await Promise.all(this._videos.map(v => loadImage(fullResStackUrl(v.stackUrl))));
      if (!imgs.some(Boolean)) throw new Error('no stack could be loaded');
      const cellW = Math.max(...imgs.filter(Boolean).map(i => i.naturalWidth));
      const cellH = Math.max(...imgs.filter(Boolean).map(i => i.naturalHeight));
      const capH = Math.max(28, Math.round(cellH / 30));
      const g = stackGridGeometry(this._videos.length, cellW, cellH, capH, this._opts?.title ? capH * 1.5 : 0);
      const canvas = document.createElement('canvas');
      canvas.width = g.width;
      canvas.height = g.height;
      const ctx = canvas.getContext('2d');
      ctx.fillStyle = '#000';
      ctx.fillRect(0, 0, g.width, g.height);
      ctx.textBaseline = 'middle';
      if (g.titleH) {
        ctx.fillStyle = '#c9d1d9';
        ctx.font = `bold ${Math.round(capH * 0.75)}px monospace`;
        ctx.fillText(this._opts.title, g.pad, g.titleH / 2);
      }
      ctx.font = `${Math.round(capH * 0.6)}px monospace`;
      g.cells.forEach((c, i) => {
        const img = imgs[i];
        ctx.fillStyle = '#8b949e';
        ctx.fillText(mdmTileCaption(this._videos[i]), c.x, c.y + capH / 2);
        const top = c.y + capH;
        if (!img) {
          ctx.fillStyle = '#111';
          ctx.fillRect(c.x, top, cellW, cellH);
          ctx.fillStyle = '#8b949e';
          ctx.textAlign = 'center';
          ctx.fillText('No stack', c.x + cellW / 2, top + cellH / 2);
          ctx.textAlign = 'start';
          return;
        }
        const k = Math.min(cellW / img.naturalWidth, cellH / img.naturalHeight);
        const w = img.naturalWidth * k, h = img.naturalHeight * k;
        ctx.drawImage(img, c.x + (cellW - w) / 2, top + (cellH - h) / 2, w, h);
      });
      const blob = await new Promise(res => canvas.toBlob(res, 'image/jpeg', 0.92));
      if (!blob) throw new Error('the browser could not encode the image');
      const slug = (this._opts?.title || '').replace(/[^a-z0-9]/gi, '_').slice(0, 24);
      saveBlob(blob, `rovimen_stacks_${slug || 'event'}.jpg`);
    } catch (e) {
      _toast('Stack download failed: ' + e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = origTxt;
    }
  }

  // ── Static registry ────────────────────────────────────────────────────────

  static _instances = new Set();

  static closeAll() {
    for (const inst of MultiDetModal._instances) inst.close();
  }
}
