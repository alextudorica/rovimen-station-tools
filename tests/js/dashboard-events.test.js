import { describe, it, expect, beforeAll, beforeEach, vi } from 'vitest';

// dashboard-events.js wires its night picker and two modals at module load,
// so give it those mount points, stub fetch, then dynamic-import it. The
// Sort & Filter tab and the compilation cart are driven through the same
// window.* entry points the page's buttons use.

const RANGE = { detections: [] };
let rangeStatus = 200;
let rangeBody = RANGE;
const fetched = [];

function jsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: String(status),
    headers: { get: () => 'application/json' },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

function el(tag, id, parent = document.body) {
  const e = document.createElement(tag);
  e.id = id;
  parent.appendChild(e);
  return e;
}

beforeAll(async () => {
  el('select', 'ev-date');
  el('div', 'ev-modal-container');
  el('div', 'ev-single-modal-container');
  el('input', 'sf-from');
  el('input', 'sf-to');
  el('div', 'sortfilter-main');
  el('div', 'sf-shower-chips');
  const sort = el('select', 'sf-sort');
  sort.innerHTML = '<option value="time_desc" selected>Newest</option>';
  el('div', 'comp-drawer-body');
  globalThis.fetch = vi.fn(async url => {
    fetched.push(url);
    if (url.startsWith('/api/detections/range')) return jsonResponse(rangeBody, rangeStatus);
    if (url === '/api/detections/nights') return jsonResponse([]);
    return jsonResponse({});
  });
  await import('../../dashboard/static/dashboard-events.js');
});

beforeEach(() => {
  rangeStatus = 200;
  rangeBody = RANGE;
  fetched.length = 0;
});

const EVIL = [
  '"><img src=x onerror="window.__xss=1">',
  "');window.__xss=1;//",
];

function detection(shower, extra = {}) {
  return {
    host_key: 'pub_station', cam: 'PUB001', date: '20261101',
    filename: 'PUB001_20261101_010101.mkv', meteor_time: '2026-11-01T01:01:05',
    station_label: 'Public', rms: { shower, mag_apparent: 1.0 }, ...extra,
  };
}

async function loadRange(from = '2026-11-01', to = '2026-11-02') {
  document.getElementById('sf-from').value = from;
  document.getElementById('sf-to').value = to;
  await window.sfReload();
}

describe('Sort & Filter shower chips', () => {
  it('render station-pushed shower codes as text, not markup', async () => {
    rangeBody = { detections: EVIL.map(s => detection(s)) };
    await loadRange();
    const chips = document.getElementById('sf-shower-chips');
    expect(chips.querySelector('img')).toBeNull();
    const buttons = [...chips.querySelectorAll('button.sf-chip')];
    expect(buttons.map(b => b.dataset.shower).sort()).toEqual([...EVIL].sort());
    for (const b of buttons) {
      // The handler reads the code back from the attribute, never from a
      // JS string literal a quote in the code could close.
      expect(b.getAttribute('onclick')).toBe('sfToggleShower(this.dataset.shower)');
      expect(b.textContent.startsWith(b.dataset.shower)).toBe(true);
    }
    expect(window.__xss).toBeUndefined();
  });
});

describe('Sort & Filter range limit', () => {
  it('refuses a range over 92 days without asking the server', async () => {
    await loadRange('2026-01-01', '2026-04-03');
    expect(fetched.some(u => u.startsWith('/api/detections/range'))).toBe(false);
    expect(document.getElementById('sortfilter-main').textContent).toContain('92 days');
  });

  it('asks the server for 92 days', async () => {
    await loadRange('2026-01-01', '2026-04-02');
    expect(fetched.some(u => u.startsWith('/api/detections/range?from=20260101&to=20260402'))).toBe(true);
  });

  it("shows the server's JSON error as text", async () => {
    rangeStatus = 400;
    rangeBody = { error: '<img src=x onerror="window.__xss=1"> Pick a range of at most 92 days.' };
    await loadRange();
    const main = document.getElementById('sortfilter-main');
    expect(main.querySelector('img')).toBeNull();
    expect(main.textContent).toContain('Pick a range of at most 92 days.');
    expect(main.textContent).not.toContain('{"error"');
  });
});

describe('compilation cart', () => {
  it('renders the label and filename of a detection as text', () => {
    const d = detection(EVIL[0], {
      filename: 'PUB001_<img src=x onerror="window.__xss=1">.mkv',
      station_label: '<img src=x onerror="window.__xss=1">',
    });
    window.compCartToggle(d);
    const body = document.getElementById('comp-drawer-body');
    expect(body.querySelector('img')).toBeNull();
    expect(body.querySelector('.comp-row-sub').textContent).toBe(d.filename);
    expect(body.querySelector('.comp-row-meta').textContent).toContain(d.station_label);
    expect(window.__xss).toBeUndefined();
    window.compCartToggle(d);
  });
});
