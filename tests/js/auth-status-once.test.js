import { describe, it, expect, vi, afterEach } from 'vitest';
import { readFileSync, readdirSync } from 'fs';
import { resolve } from 'path';

import { templateBody } from './helpers.js';

// Every page asks /api/auth/status once. The shared header (dashboard-common.js
// initNavAuth), the page's own init, the clip controls, the basket and the
// Hall of Fame toggle all await one memoised request (fetchAuthStatus), where
// each used to send its own: the home page asked four times.
//
// A page load is simulated from its template: the shared nav and the page
// body go into the DOM, then the modules its <script type="module"> tags name
// are imported in order, each test on a fresh module graph.

const ROOT = resolve(import.meta.dirname, '..', '..');
const TEMPLATES = resolve(ROOT, 'dashboard', 'templates');

/** The shared header markup with its Jinja stripped. */
function navMarkup() {
  return readFileSync(resolve(TEMPLATES, '_nav.html'), 'utf8')
    .replace(/\{#[\s\S]*?#\}/g, '')
    .replace(/\{%[\s\S]*?%\}/g, '')
    .replace(/\{\{[\s\S]*?\}\}/g, '');
}

/** Module scripts a template loads, in page order. */
function pageModules(name) {
  const src = readFileSync(resolve(TEMPLATES, name), 'utf8');
  return [...src.matchAll(/<script type="module" src="\{\{ url_for\('static', filename='([a-z0-9-]+\.js)'/g)]
    .map(m => m[1]);
}

const PAGES = readdirSync(TEMPLATES)
  .filter(n => n.endsWith('.html') && !n.startsWith('_'))
  .filter(n => readFileSync(resolve(TEMPLATES, n), 'utf8').includes('{% include "_nav.html" %}'))
  .sort();

function jsonResponse(body) {
  return {
    ok: true, status: 200, statusText: 'OK',
    headers: { get: () => 'application/json' },
    json: async () => body,
    text: async () => JSON.stringify(body),
    blob: async () => new Blob([]),
  };
}

const AUTH = { user: 'operator', role: 'admin', admin: true, stations: [] };
let authCalls = 0;

/** The browser APIs the header and pages use that jsdom does not have. */
function stubBrowser() {
  const mql = { matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} };
  vi.stubGlobal('matchMedia', () => mql);
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal('IntersectionObserver', class { observe() {} unobserve() {} disconnect() {} });
}

function stubFetch() {
  authCalls = 0;
  vi.stubGlobal('fetch', vi.fn(async (url) => {
    if (String(url) === '/api/auth/status') {
      authCalls += 1;
      return jsonResponse(AUTH);
    }
    return jsonResponse({});
  }));
}

async function settle() {
  for (let i = 0; i < 20; i++) await new Promise(r => setTimeout(r, 0));
}

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = '';
});

describe('one /api/auth/status request per page load', () => {
  it('finds the pages that carry the shared header', () => {
    expect(PAGES).toContain('overview.html');
    expect(PAGES).toContain('dashboard.html');
    expect(PAGES.length).toBeGreaterThanOrEqual(10);
  });

  it.each(PAGES)('%s', async (page) => {
    vi.resetModules();
    stubBrowser();
    stubFetch();
    document.body.innerHTML = navMarkup() + templateBody(page);
    const failed = [];
    for (const mod of pageModules(page)) {
      try {
        await import(`../../dashboard/static/${mod}`);
      } catch (e) {
        failed.push(`${mod}: ${e.message}`);
      }
    }
    // Every module the page loads ran, so its requests are all counted.
    expect(failed).toEqual([]);
    await settle();
    // The header rendered the account from that one answer...
    expect(document.querySelector('#nav-auth .nav-user-btn')?.textContent).toContain('operator');
    // ...and so did everything else on the page.
    expect(authCalls).toBe(1);

    // Opening a clip asks the basket and Hall of Fame toggles; they reuse it.
    const basket = await import('../../dashboard/static/dashboard-basket-add.js');
    const hof = await import('../../dashboard/static/dashboard-hof-add.js');
    const common = await import('../../dashboard/static/dashboard-common.js');
    await basket.basketAuth();
    await hof.hofIsAdmin();
    await common.resolveUserRole();
    expect(authCalls).toBe(1);
  });
});
