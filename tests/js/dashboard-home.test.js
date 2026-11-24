import { describe, it, expect, beforeAll, vi } from 'vitest';

// The home page ("/") runs dashboard-home.js: the station and camera counts,
// the network numbers and the latest plots per camera. It must not load the
// Network overview's module (map, FOV geometry, live view, GMN orbit layer,
// status table) or ask for that page's data. A separate file so the module's
// start-up runs against this page's DOM and fetch stub.

const loaded = vi.hoisted(() => []);
vi.mock('../../dashboard/static/dashboard-overview.js', () => { loaded.push('dashboard-overview.js'); return {}; });
vi.mock('../../dashboard/static/dashboard-map-point.js', () => { loaded.push('dashboard-map-point.js'); return {}; });

const fetched = [];

const OVERVIEW = {
  stn_a: { online: true, label: 'A', cameras: [{ code: 'AA0001' }, { code: 'AA0002' }] },
  stn_b: { online: false, label: 'B', cameras: [{ code: 'BB0001' }] },
  stn_c: { online: true, label: 'C', status: 'commissioning', cameras: [{ code: 'CC0001' }] },
};
const STACKS = [
  { host: 'stn_a', cam: 'AA0001', date: '20261120', filename: 'AA0001_stack.jpg' },
  { host: 'stn_a', cam: 'AA0002', date: '20261120', filename: 'AA0002_stack.jpg' },
];

function jsonResponse(body) {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
}

let home;
beforeAll(async () => {
  for (const id of ['ov-summary', 'ov-net-stats', 'ov-stacks-grid', 'ov-plot-toggle-bar', 'ov-plot-modal', 'ov-plot-modal-title']) {
    const div = document.createElement('div');
    div.id = id;
    document.body.appendChild(div);
  }
  const plotImg = document.createElement('img');
  plotImg.id = 'ov-plot-modal-img';
  document.body.appendChild(plotImg);
  const dl = document.createElement('a');
  dl.id = 'ov-plot-modal-dl';
  document.body.appendChild(dl);
  globalThis.fetch = vi.fn(async url => {
    fetched.push(String(url));
    if (url === '/api/overview') return jsonResponse(OVERVIEW);
    if (url === '/api/overview/stacks') return jsonResponse(STACKS);
    if (url === '/api/network-stats') return jsonResponse({ total_covered_area_km2: 12345 });
    return jsonResponse({});
  });
  home = await import('../../dashboard/static/dashboard-home.js');
});

describe('home page start-up', () => {
  it('loads without the Network overview module', () => {
    expect(loaded).toEqual([]);
    expect(window.toggleAllFov).toBeUndefined();
    expect(window.nwSort).toBeUndefined();
  });

  it('renders the station and camera counts', async () => {
    await vi.waitFor(() => expect(document.querySelector('#ov-summary .ov-summary-cell')).not.toBeNull());
    const cells = [...document.querySelectorAll('#ov-summary .ov-summary-cell')].map(c => c.textContent);
    // Stations online / commissioning / offline, then cameras.
    expect(cells).toEqual(['1', '1', '1', '2', '1', '1']);
  });

  it('renders the network numbers and one card per station with stacks', async () => {
    await vi.waitFor(() => expect(document.getElementById('ov-net-stats').textContent).toContain('12,345'));
    await vi.waitFor(() => expect(document.querySelectorAll('#ov-stacks-grid .ov-station-group')).toHaveLength(3));
    expect(document.getElementById('ov-cam-img-stn_a-AA0001')).not.toBeNull();
  });

  it('asks only for the home page data', async () => {
    await vi.waitFor(() => expect(fetched).toContain('/api/network-stats'));
    for (const url of fetched) {
      expect(url).not.toMatch(/^\/api\/(stations|status\/all|platepar\/|overview\/station_orbits|gmn|latest_ff_maxpixel)/);
    }
  });

  it('exposes the handlers the home template and its cards call', () => {
    for (const name of ['ovUpdateZoom', 'ovPlotOpen', 'ovPlotClose', 'ovGlobalPlotSwitch', 'ovOpenCardImg', 'openAppPanel', 'appPanelClose']) {
      expect(typeof window[name], name).toBe('function');
    }
  });

  it('opens a card in the plot modal and closes it on Escape', async () => {
    const img = await vi.waitFor(() => {
      const el = document.getElementById('ov-cam-img-stn_a-AA0001');
      if (!el) throw new Error('card not rendered');
      return el;
    });
    window.ovOpenCardImg(img.id, 'stn_a', 'AA0001', '2026-11-20');
    const modal = document.getElementById('ov-plot-modal');
    expect(modal.classList.contains('open')).toBe(true);
    expect(document.getElementById('ov-plot-modal-title').textContent).toBe('AA0001 · 2026-11-20 · Captured stack');
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(modal.classList.contains('open')).toBe(false);
  });

  it('lets the browser back gesture close a shared video modal', () => {
    expect(typeof window._VideoModalCloseAll).toBe('function');
  });
});

/** Newest archived RMS plots for many cameras in one request. */
describe('ovFetchLatestPlots', () => {
  const targets = [
    { host: 'gmnro07', cam: 'RO000W' }, { host: 'gmnro07', cam: 'RO000X' },
    { host: 'gmnro07', cam: 'RO000W' },   // duplicate: asked once
  ];
  const ok = body => ({ ok: true, json: async () => body });

  it('asks for every camera in one batched request', async () => {
    const calls = [];
    const fetchFn = async url => {
      calls.push(url);
      return ok({ 'gmnro07/RO000W': { date: '20260920', plots: [] } });
    };
    const out = await home.ovFetchLatestPlots(targets, fetchFn);
    expect(calls).toEqual(['/api/rms/plots-latest?cams=gmnro07%2FRO000W,gmnro07%2FRO000X']);
    expect(out.get('gmnro07/RO000W').date).toBe('20260920');
    expect(out.has('gmnro07/RO000X')).toBe(false);
  });

  it('falls back to one request per camera when the batch fails', async () => {
    const calls = [];
    const fetchFn = async url => {
      calls.push(url);
      if (url.includes('?cams=')) return { ok: false, status: 404 };
      return ok({ date: '20260919', plots: [], url });
    };
    const out = await home.ovFetchLatestPlots(targets, fetchFn);
    expect(calls.slice(1).sort()).toEqual([
      '/api/rms/plots-latest/gmnro07/RO000W', '/api/rms/plots-latest/gmnro07/RO000X']);
    expect(out.get('gmnro07/RO000X').date).toBe('20260919');
  });

  it('makes no request for no cameras', async () => {
    let called = false;
    const out = await home.ovFetchLatestPlots([], async () => { called = true; });
    expect(called).toBe(false);
    expect(out.size).toBe(0);
  });
});
