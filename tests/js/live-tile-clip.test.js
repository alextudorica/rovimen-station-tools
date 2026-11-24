import { describe, it, expect, beforeAll, beforeEach, afterEach, vi } from 'vitest';

// Live-tile click (overview "Live Cameras"): the still shows at once, the
// clip replaces it when the station has rendered it, and every wait ends in
// either playback or a visible message with a Retry button.

let ctx;
beforeAll(async () => {
  const mount = document.createElement('div');
  mount.id = 'gmn-clip-modal-container';
  document.body.appendChild(mount);
  ctx = await import('../../dashboard/static/dashboard-overview.js');
});

const HOST = 'gmnde01';
const CAM = 'DE0018';
const FN = 'DE0018_20260925_190342_color.mkv';
const chunk = (extra) => ({
  date: '20260925', filename: FN, time: '19:03:42',
  video_url: `/video/${HOST}/${CAM}/20260925/${FN}`, ...extra,
});

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status, headers: { 'content-type': 'application/json' },
  });
}

function setupDom() {
  document.getElementById('ov-live-modal')?.remove();
  document.getElementById(`live-${HOST}-${CAM}`)?.remove();
  const modal = document.createElement('div');
  modal.id = 'ov-live-modal';
  modal.className = 'ov-live-modal-wrap';
  modal.innerHTML =
    '<img id="ov-live-still" style="display:none">' +
    '<video id="ov-live-video" style="display:none"></video>' +
    '<div id="ov-live-video-label"></div><div id="ov-live-status"></div>';
  document.body.appendChild(modal);
  const card = document.createElement('div');
  card.id = `live-${HOST}-${CAM}`;
  card.dataset.ts = new Date().toISOString();
  const img = document.createElement('img');
  img.src = 'blob:http://localhost/tile-frame';
  card.appendChild(img);
  document.body.appendChild(card);
}

const $ = (id) => document.getElementById(id);

describe('_liveOpenVideo', () => {
  let calls;
  beforeEach(() => {
    vi.useFakeTimers();
    setupDom();
    calls = [];
    vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
    vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
    vi.spyOn(window, 'scrollTo').mockImplementation(() => {});
  });
  afterEach(() => {
    window.closeLiveModal();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  function mockFetch(responder) {
    globalThis.fetch = vi.fn((url) => {
      calls.push(url);
      return Promise.resolve(responder(url, calls.length));
    });
  }

  it('opens on the tile still before the station answers', () => {
    mockFetch(() => new Promise(() => {}));   // never resolves
    ctx._liveOpenVideo(HOST, CAM);
    expect($('ov-live-modal').classList.contains('open')).toBe(true);
    expect($('ov-live-still').style.display).toBe('');
    expect($('ov-live-still').src).toBe('blob:http://localhost/tile-frame');
    expect($('ov-live-video').style.display).toBe('none');
    expect($('ov-live-status').textContent).toMatch(/Loading the latest clip/);
  });

  it('plays an overlaid chunk and swaps the still for the video', async () => {
    mockFetch(() => jsonResponse(chunk({ overlaid: true, rendering: false })));
    ctx._liveOpenVideo(HOST, CAM);
    await vi.advanceTimersByTimeAsync(0);
    const video = $('ov-live-video');
    expect(video.getAttribute('src')).toBe(chunk().video_url);
    video.oncanplay();
    expect(video.style.display).toBe('');
    expect($('ov-live-still').style.display).toBe('none');
    expect($('ov-live-video-label').textContent).toMatch(/19:03:42 UTC/);
  });

  it('polls the same chunk while the station renders, then plays it', async () => {
    mockFetch((url, n) => jsonResponse(chunk({ overlaid: n >= 3, rendering: true })));
    ctx._liveOpenVideo(HOST, CAM);
    await vi.advanceTimersByTimeAsync(0);
    expect($('ov-live-status').textContent).toMatch(/burning in the overlay/);
    expect($('ov-live-video').getAttribute('src')).toBeNull();
    await vi.advanceTimersByTimeAsync(ctx.LIVE_CLIP_POLL_MS * 2);
    expect(calls[1]).toBe(`/api/latest_chunk/${HOST}/${CAM}?date=20260925&filename=${FN}`);
    expect($('ov-live-video').getAttribute('src')).toBe(chunk().video_url);
  });

  it('gives up with a message and a Retry button, keeping the still', async () => {
    mockFetch(() => jsonResponse(chunk({ overlaid: false, rendering: true })));
    ctx._liveOpenVideo(HOST, CAM);
    await vi.advanceTimersByTimeAsync(ctx.LIVE_CLIP_WAIT_MS + ctx.LIVE_CLIP_POLL_MS * 2);
    const status = $('ov-live-status');
    expect(status.classList.contains('error')).toBe(true);
    expect(status.textContent).toMatch(/has not finished preparing/);
    expect(status.querySelector('button').textContent).toBe('Retry');
    expect($('ov-live-still').style.display).toBe('');
    const n = calls.length;
    await vi.advanceTimersByTimeAsync(ctx.LIVE_CLIP_POLL_MS * 4);
    expect(calls.length).toBe(n);   // polling stopped
  });

  it('bounds the wait for an overlaid clip that never starts', async () => {
    mockFetch(() => jsonResponse(chunk({ overlaid: true })));
    ctx._liveOpenVideo(HOST, CAM);
    await vi.advanceTimersByTimeAsync(ctx.LIVE_CLIP_LOAD_MS + 10);
    expect($('ov-live-status').textContent).toMatch(/did not start within 45 s/);
    expect($('ov-live-video').getAttribute('src')).toBeNull();
  });

  it('loads directly when the dashboard could not start a render', async () => {
    mockFetch(() => jsonResponse(chunk({ overlaid: false, rendering: false })));
    ctx._liveOpenVideo(HOST, CAM);
    await vi.advanceTimersByTimeAsync(0);
    expect($('ov-live-video').getAttribute('src')).toBe(chunk().video_url);
    expect(calls.length).toBe(1);
  });

  it('says so when the camera has no recent clip', async () => {
    mockFetch(() => jsonResponse({ error: 'no chunks available' }, 404));
    ctx._liveOpenVideo(HOST, CAM);
    await vi.advanceTimersByTimeAsync(0);
    expect($('ov-live-status').textContent).toMatch(/No recent clip on DE0018/);
  });

  it('closing the modal stops polling', async () => {
    mockFetch(() => jsonResponse(chunk({ overlaid: false, rendering: true })));
    ctx._liveOpenVideo(HOST, CAM);
    await vi.advanceTimersByTimeAsync(0);
    window.closeLiveModal();
    const n = calls.length;
    await vi.advanceTimersByTimeAsync(ctx.LIVE_CLIP_POLL_MS * 4);
    expect(calls.length).toBe(n);
  });
});
