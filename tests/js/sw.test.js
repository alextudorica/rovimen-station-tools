import { describe, it, expect, beforeEach } from 'vitest';
import { existsSync, readFileSync } from 'fs';
import { resolve } from 'path';
import { Script, createContext } from 'vm';

// The service worker is registered as a classic script
// (navigator.serviceWorker.register('/sw.js')), so it is loaded here the same
// way: as plain script text in its own context, with fake caches and fetch.
// An `export` in the file (added for the old tests on 2026-06-03) made every
// install and update fail to parse, so no browser picked up a new worker.

const ROOT = resolve(import.meta.dirname, '..', '..');
const SW_PATH = resolve(ROOT, 'dashboard/static/sw.js');
const SOURCE = readFileSync(SW_PATH, 'utf8');
const VERSION = 'abc1234';
// What the /sw.js route serves (dashboard/routes/misc.py).
const SERVED = SOURCE.replaceAll('__BUILD_VERSION__', VERSION);

class FakeCache {
  constructor() { this.entries = new Map(); }
  async match(req) { return this.entries.get(typeof req === 'string' ? req : req.url) || undefined; }
  async put(req, resp) { this.entries.set(typeof req === 'string' ? req : req.url, resp); }
  async addAll(urls) { for (const u of urls) this.entries.set(u, { status: 200 }); }
}

class FakeCacheStorage {
  constructor(names = []) { this.store = new Map(names.map(n => [n, new FakeCache()])); }
  async open(name) {
    if (!this.store.has(name)) this.store.set(name, new FakeCache());
    return this.store.get(name);
  }
  async keys() { return [...this.store.keys()]; }
  async delete(name) { return this.store.delete(name); }
}

function loadWorker({ cacheNames = [] } = {}) {
  const listeners = {};
  const fetched = [];
  const ctx = {
    self: {
      location: { origin: 'https://dash.example' },
      addEventListener: (type, fn) => { listeners[type] = fn; },
      skipWaiting: async () => {},
      clients: { claim: async () => {} },
    },
    caches: new FakeCacheStorage(cacheNames),
    fetch: async (req) => {
      fetched.push(typeof req === 'string' ? req : req.url);
      return { status: 200, type: 'basic', clone() { return this; } };
    },
    Response: class { constructor(body, init) { this.body = body; Object.assign(this, init); } },
    URL,
    console,
  };
  createContext(ctx);
  const exposed = new Script(`${SERVED}\n;({ CACHE_NAME, SHELL_URLS })`).runInContext(ctx);
  return { ctx, listeners, fetched, ...exposed };
}

function fetchEvent(url, { method = 'GET', mode = 'cors' } = {}) {
  const ev = { request: { url, method, mode }, responded: null };
  ev.respondWith = (p) => { ev.responded = p; };
  return ev;
}

describe('sw.js source', () => {
  it('parses as a classic script (no import/export)', () => {
    expect(() => new Script(SOURCE)).not.toThrow();
  });

  it('carries the build-version token the /sw.js route replaces, once', () => {
    expect(SOURCE.match(/__BUILD_VERSION__/g)).toHaveLength(1);
    expect(SOURCE).toContain("const CACHE_VERSION = '__BUILD_VERSION__';");
  });

  it('pre-caches only files that exist, so the install cannot fail', () => {
    const { SHELL_URLS } = loadWorker();
    expect(SHELL_URLS.length).toBeGreaterThan(0);
    for (const url of SHELL_URLS) {
      expect(url.startsWith('/static/')).toBe(true);
      expect(existsSync(resolve(ROOT, 'dashboard', url.slice(1)))).toBe(true);
    }
  });
});

describe('fetch routing', () => {
  let sw;
  beforeEach(() => { sw = loadWorker(); });

  const handled = (url, opts) => {
    const ev = fetchEvent(url, opts);
    sw.listeners.fetch(ev);
    return ev.responded !== null;
  };

  it('serves /static/ files through the cache', () => {
    expect(handled('https://dash.example/static/dashboard.css?v=abc')).toBe(true);
    expect(handled('https://dash.example/static/dashboard-common.js?v=abc')).toBe(true);
    expect(handled('https://dash.example/static/img/favicon.svg')).toBe(true);
  });

  it('leaves everything else to the network', () => {
    for (const path of [
      '/',
      '/sw.js',
      '/static',
      '/api/stations',
      '/stack/gmnro05/RO0003/20261120/RO0003_20261120_201500_stack.webp',
      '/fullstack/gmnro05/RO0003/20261120/RO0003_20261120_201500_stack.webp',
      '/shortclip/gmnro05/RO0003/20261120/RO0003_20261120_201500_color.mkv',
      '/loop_clip/gmnro05/RO0003/20261120/x.mp4',
      '/media/v1/clip/RO0003/2026-11-20/x.mp4',
      '/thumbnail/gmnro05/RO0003/x.webp',
      '/video/gmnro05/RO0003/x.mp4',
      '/highlights',
    ]) {
      expect(handled(`https://dash.example${path}`), path).toBe(false);
    }
  });

  it('never touches navigations, non-GETs or other origins', () => {
    expect(handled('https://dash.example/static/x.js', { mode: 'navigate' })).toBe(false);
    expect(handled('https://dash.example/events', { mode: 'navigate' })).toBe(false);
    expect(handled('https://dash.example/static/x.js', { method: 'POST' })).toBe(false);
    expect(handled('https://unpkg.com/static/leaflet.js')).toBe(false);
  });

  it('answers a cached static file from the cache and refreshes it behind', async () => {
    const cache = await sw.ctx.caches.open(sw.CACHE_NAME);
    const url = 'https://dash.example/static/dashboard.css?v=abc';
    const cached = { status: 200, marker: 'cached' };
    await cache.put(url, cached);
    const ev = fetchEvent(url);
    sw.listeners.fetch(ev);
    expect(await ev.responded).toBe(cached);
    expect(sw.fetched).toContain(url);
  });
});

describe('install and activate', () => {
  it('names the cache after the build version', () => {
    expect(loadWorker().CACHE_NAME).toBe(`rovimen-shell-${VERSION}`);
  });

  it('a new version ends with only its own rovimen cache', async () => {
    const sw = loadWorker({
      cacheNames: ['rovimen-shell-v36', 'rovimen-shell-1763800000', 'someone-elses-cache'],
    });
    let installed;
    sw.listeners.install({ waitUntil: (p) => { installed = p; } });
    await installed;
    let activated;
    sw.listeners.activate({ waitUntil: (p) => { activated = p; } });
    await activated;
    expect((await sw.ctx.caches.keys()).sort()).toEqual(
      [`rovimen-shell-${VERSION}`, 'someone-elses-cache'].sort());
  });
});
