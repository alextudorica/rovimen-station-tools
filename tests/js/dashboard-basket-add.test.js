import { describe, it, expect, vi, beforeEach } from 'vitest';

// ── Download-basket module (dashboard-basket-add.js) ──────────────────────────
//
// The module resolves the session user via /api/auth/status, caches it for the
// page lifetime, and mounts the floating basket widget at import time — so
// every test stubs fetch FIRST, then dynamically imports a fresh module copy
// (vi.resetModules) to reset the user/items caches.

// The stub itself: importing the module loads dashboard-common.js, which
// wraps window.fetch, so the spy is not what globalThis.fetch ends up as.
let fetchStub = null;

function stubFetch(routes) {
  const calls = [];
  vi.stubGlobal('fetch', fetchStub = vi.fn(async (url, opts = {}) => {
    const key = `${opts.method || 'GET'} ${url}`;
    calls.push(key);
    const hit = routes[key];
    if (hit === undefined) return { ok: false, status: 404, json: async () => ({}) };
    return { ok: true, status: 200, json: async () => hit };
  }));
  return calls;
}

async function importFresh() {
  vi.resetModules();
  return import('../../dashboard/static/dashboard-basket-add.js');
}

beforeEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = '';
});

describe('basketKey', () => {
  it('normalises compact dates so VDB and Highlights clips match', async () => {
    stubFetch({});
    const { basketKey } = await importFresh();
    const a = basketKey({ camera: 'ro0001', date: '20260701', filename: 'x.mkv' });
    const b = basketKey({ camera: 'RO0001', date: '2026-07-01', filename: 'x.mkv' });
    expect(a).toBe(b);
    expect(a).toBe('RO0001|2026-07-01|x.mkv');
  });
});

describe('wireBasketButton', () => {
  const CLIP = {
    camera: 'PUB001', date: '20260701',
    filename: 'PUB001_20260701_220001_color.mkv',
  };

  it('hides the button for anonymous sessions', async () => {
    stubFetch({ 'GET /api/auth/status': { user: null } });
    const { wireBasketButton } = await importFresh();
    const btn = document.createElement('button');
    await wireBasketButton(btn, CLIP);
    expect(btn.style.display).toBe('none');
  });

  it('hides the button for clips without a full identity', async () => {
    stubFetch({ 'GET /api/auth/status': { user: 'alice' } });
    const { wireBasketButton } = await importFresh();
    const btn = document.createElement('button');
    await wireBasketButton(btn, { camera: 'PUB001' });   // no date/filename
    expect(btn.style.display).toBe('none');
  });

  it('paints add state and POSTs the clip on click', async () => {
    const calls = stubFetch({
      'GET /api/auth/status': { user: 'alice' },
      'GET /api/basket': { items: [] },
      'POST /api/basket': { item: { id: 'bk_000000000001' } },
    });
    const { wireBasketButton } = await importFresh();
    const btn = document.createElement('button');
    await wireBasketButton(btn, CLIP);
    expect(btn.style.display).toBe('');
    expect(btn.textContent).toContain('Add to basket');
    await btn.onclick();
    expect(calls).toContain('POST /api/basket');
    expect(btn.textContent).toContain('In basket');
  });

  it('paints in-basket state when the clip is already stored', async () => {
    stubFetch({
      'GET /api/auth/status': { user: 'alice' },
      'GET /api/basket': { items: [{
        id: 'bk_000000000001', camera: 'PUB001', date: '2026-07-01',
        filename: 'PUB001_20260701_220001_color.mkv',
      }] },
    });
    const { wireBasketButton } = await importFresh();
    const btn = document.createElement('button');
    await wireBasketButton(btn, CLIP);
    expect(btn.textContent).toContain('In basket');
    expect(btn.classList.contains('vm-basket-active')).toBe(true);
  });
});

describe('trim payload', () => {
  const CLIP = {
    camera: 'PUB001', date: '20260701',
    filename: 'PUB001_20260701_220001_color.mkv',
  };
  const ROUTES = {
    'GET /api/auth/status': { user: 'alice' },
    'GET /api/basket': { items: [] },
    'POST /api/basket': { item: { id: 'bk_000000000001' } },
  };

  function postedBody() {
    const call = fetchStub.mock.calls
      .find(([url, opts]) => opts?.method === 'POST' && url === '/api/basket');
    return JSON.parse(call[1].body);
  }

  it('carries trim_start/trim_end for a proper sub-range, at 0.1s precision', async () => {
    stubFetch(ROUTES);
    const { basketToggle } = await importFresh();
    await basketToggle({ ...CLIP,
      getTrim: () => ({ start: 12.31, end: 15.44, duration: 20 }) });
    const body = postedBody();
    expect(body.trim_start).toBe(12.3);
    expect(body.trim_end).toBe(15.4);
  });

  it('omits trim fields when the handles span the whole clip', async () => {
    stubFetch(ROUTES);
    const { basketToggle } = await importFresh();
    await basketToggle({ ...CLIP,
      getTrim: () => ({ start: 0, end: 20, duration: 20 }) });
    const body = postedBody();
    expect('trim_start' in body).toBe(false);
    expect('trim_end' in body).toBe(false);
  });

  it('treats handle jitter within epsilon of full range as untrimmed', async () => {
    stubFetch(ROUTES);
    const { basketToggle } = await importFresh();
    await basketToggle({ ...CLIP,
      getTrim: () => ({ start: 0.03, end: 19.98, duration: 20 }) });
    expect('trim_start' in postedBody()).toBe(false);
  });

  it('posts the exact legacy payload when no trim source is wired', async () => {
    stubFetch(ROUTES);
    const { basketToggle } = await importFresh();
    await basketToggle(CLIP);
    expect(Object.keys(postedBody()).sort()).toEqual(
      ['camera', 'date', 'filename', 'stack', 'station_label']);
  });
});

describe('floating widget', () => {
  it('mounts with a badge for a signed-in session', async () => {
    stubFetch({
      'GET /api/auth/status': { user: 'alice' },
      'GET /api/basket': { items: [{ id: 'bk_000000000001', camera: 'PUB001',
        date: '2026-07-01', filename: 'PUB001_20260701_220001_color.mkv' }] },
    });
    await importFresh();
    await vi.waitFor(() => {
      expect(document.getElementById('basket-fab')).toBeTruthy();
    });
    await vi.waitFor(() => {
      expect(document.getElementById('basket-fab-badge').textContent).toBe('1');
    });
  });

  it('does not mount for anonymous sessions', async () => {
    stubFetch({ 'GET /api/auth/status': { user: null } });
    await importFresh();
    // Give the async init a tick to (not) run.
    await new Promise(r => setTimeout(r, 10));
    expect(document.getElementById('basket-fab')).toBeNull();
  });
});

describe('fireball pack checkbox (admin-only)', () => {
  const ITEMS = { items: [{ id: 'bk_000000000001', camera: 'PUB001',
    date: '2026-07-01', filename: 'PUB001_20260701_220001_color.mkv' }] };

  it('is visible for admins', async () => {
    stubFetch({
      'GET /api/auth/status': { user: 'operator', role: 'admin', admin: true },
      'GET /api/basket': ITEMS,
    });
    await importFresh();
    await vi.waitFor(() => {
      expect(document.getElementById('basket-fireball-row')).toBeTruthy();
    });
    expect(document.getElementById('basket-fireball-row').style.display).toBe('');
  });

  it('stays hidden for non-admin accounts', async () => {
    stubFetch({
      'GET /api/auth/status': { user: 'bob', role: 'visitor' },
      'GET /api/basket': ITEMS,
    });
    await importFresh();
    await vi.waitFor(() => {
      expect(document.getElementById('basket-fireball-row')).toBeTruthy();
    });
    expect(document.getElementById('basket-fireball-row').style.display).toBe('none');
  });
});
