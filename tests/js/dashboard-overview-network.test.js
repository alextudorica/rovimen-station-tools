import { describe, it, expect, beforeAll, vi } from 'vitest';

// The Network overview ("/network") runs dashboard-overview.js. Its top bar
// (counts, network numbers) is drawn by the renderers it shares with the home
// page; the live bar note, the status table and the station list stay its
// own. It never asks for the home page's stacks or plots. A separate file so
// the module's start-up runs against this page's DOM and fetch stub; the map
// is left out because jsdom has no Leaflet.

const fetched = [];

const OVERVIEW = {
  stn_a: { online: true, label: 'A', cameras: [
    { code: 'AA0001', capture: { state: 'recording' } }, { code: 'AA0002', capture: { state: 'stalled' } }] },
  stn_b: { online: false, label: 'B', cameras: [{ code: 'BB0001', capture: { state: 'offline' } }] },
};

function jsonResponse(body) {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
}

beforeAll(async () => {
  document.body.innerHTML = `
    <div id="ov-summary"></div><div id="ov-net-stats"></div>
    <button id="ov-live-bar-btn"></button><span id="ov-live-bar-note"></span>
    <div id="nw-section"><div id="nw-summary"></div>
      <table class="nw-table"><thead><tr><th data-col="key" aria-sort="none" tabindex="0">Station</th></tr></thead>
      <tbody id="nw-tbody"></tbody></table><div id="nw-cards"></div><span id="nw-last-fetch"></span></div>
    <div id="gmn-clip-modal-container"></div>`;
  globalThis.fetch = vi.fn(async url => {
    fetched.push(String(url));
    if (url === '/api/overview') return jsonResponse(OVERVIEW);
    if (url === '/api/network-stats') return jsonResponse({ total_covered_area_km2: 54321 });
    return jsonResponse({});
  });
  await import('../../dashboard/static/dashboard-overview.js');
});

describe('Network overview start-up', () => {
  it('renders the shared counts and its own live note and status table', async () => {
    await vi.waitFor(() => expect(document.querySelectorAll('#nw-tbody tr')).toHaveLength(2));
    const cells = [...document.querySelectorAll('#ov-summary .ov-summary-cell')].map(c => c.textContent);
    expect(cells).toEqual(['1', '0', '1', '2', '0', '1']);
    expect(document.getElementById('ov-live-bar-note').textContent).toBe('1 of 3 cameras recording (1 stalled, 1 not reporting)');
    expect(document.getElementById('ov-live-bar-btn').disabled).toBe(false);
    await vi.waitFor(() => expect(document.getElementById('ov-net-stats').textContent).toContain('54,321'));
  });

  it('asks for the network data and never for the home page stacks or plots', () => {
    for (const url of ['/api/stations', '/api/overview', '/api/status/all', '/api/network-stats']) {
      expect(fetched).toContain(url);
    }
    expect(fetched.filter(u => u.startsWith('/api/overview/stacks') || u.startsWith('/api/rms/plots-latest'))).toEqual([]);
  });

  it('exposes its own handlers and none of the home page ones', () => {
    for (const name of ['toggleAllFov', 'nwSort', 'toggleLiveView', 'closeLiveModal', 'togglePointPick', 'appPanelClose']) {
      expect(typeof window[name], name).toBe('function');
    }
    for (const name of ['ovUpdateZoom', 'ovPlotClose', 'ovGlobalPlotSwitch', 'ovOpenCardImg']) {
      expect(window[name], name).toBeUndefined();
    }
  });
});
