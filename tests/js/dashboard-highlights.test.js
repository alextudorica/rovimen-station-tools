import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from 'vitest';

import { templateBody } from './helpers.js';

// dashboard-highlights.js wires its page at import, so mount the real
// highlights.html body first, then import it.

let ctx;
beforeAll(async () => {
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, headers: { get: () => null }, json: async () => ({}),
  })));
  document.body.innerHTML = templateBody('highlights.html');
  ctx = await import('../../dashboard/static/dashboard-highlights.js');
});

// A report card cycles its figures; one that fails to load is dropped from
// the cycle. Once every other figure had failed, the list was empty and the
// tick kept requesting slides[NaN] ("undefined") every 3 s.
describe('report card slideshow', () => {
  let requested;

  beforeEach(() => {
    requested = [];
    vi.useFakeTimers();
    vi.stubGlobal('matchMedia', () => ({ matches: false }));
    vi.stubGlobal('IntersectionObserver', class {
      constructor(cb) { this.cb = cb; }
      observe(el) { this.cb([{ target: el, isIntersecting: true }]); }
      disconnect() {}
    });
    vi.stubGlobal('Image', class {
      set src(v) { requested.push(v); }
      decode() { return Promise.reject(new Error('404')); }
    });
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  function card(slides) {
    const grid = document.createElement('div');
    grid.innerHTML = `<button class="hl-rcard-media" data-slides='${JSON.stringify(slides)}'>
      <img src="${slides[0]}"></button>`;
    document.body.appendChild(grid);
    return grid;
  }

  it('stops cycling once fewer than two figures are left', async () => {
    const grid = card(['/f/a.webp', '/f/b.webp', '/f/c.webp']);
    ctx.startSlideshows(grid);
    await vi.advanceTimersByTimeAsync(3000 * 10);
    expect(requested).toEqual(['/f/b.webp', '/f/c.webp']);
    expect(requested).not.toContain(undefined);
    grid.remove();
  });

  it('keeps cycling figures that load', async () => {
    vi.stubGlobal('Image', class {
      set src(v) { requested.push(v); }
      decode() { return Promise.resolve(); }
    });
    const grid = card(['/f/a.webp', '/f/b.webp']);
    ctx.startSlideshows(grid);
    await vi.advanceTimersByTimeAsync(3000 * 4);
    expect(requested).toEqual(['/f/b.webp', '/f/a.webp', '/f/b.webp', '/f/a.webp']);
    grid.remove();
  });
});
