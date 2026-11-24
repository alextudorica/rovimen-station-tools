import { describe, it, expect, beforeAll, beforeEach, afterEach, vi } from 'vitest';

// The Spectro tab replaced a flat text listing of every event on the Final
// Data Products tab (2026-09-19): 126 rows of ids and links, most of them
// reading "no reduced spectrum". What is asserted here is the part that made
// it a gallery — which pictures an event has, which events a filter keeps, and
// that the lightbox index of a thumbnail addresses the image the user clicked.

let ctx;
beforeAll(async () => {
  const pane = document.createElement('div');
  pane.id = 'pane-spectro';
  document.body.appendChild(pane);
  window.canAccessTab = () => true;
  window._accessBanner = t => `<div class="banner">${t}</div>`;
  ctx = await import('../../dashboard/static/dashboard-spectro.js');
});

beforeEach(() => {
  ctx._spectroState.filter = 'all';
});

const EV = (over = {}) => ({
  id: 'RO000A_20260918_212418', time: '21:24:18', trigger: 'rms',
  source: 'RO000A', state: 'none', files: ['event.json', 'reduction.json'],
  ...over,
});

describe('_imagesFor', () => {
  it('lists the pictures an event actually has, trigger frame first', () => {
    const imgs = ctx._imagesFor('GH662S', '20260918', EV({
      files: ['spectrum.png', 'wide.jpg', 'spectro.jpg', 'event.json'],
    }));
    expect(imgs.map(i => i.kind)).toEqual(['wide', 'spectro', 'spectrum']);
    expect(imgs[0].url).toContain('/spectro/RO000A_20260918_212418__wide.jpg');
  });

  it('returns nothing for an event cut before previews existed', () => {
    expect(ctx._imagesFor('GH662S', '20260918', EV())).toEqual([]);
  });

  it('percent-encodes the camera, date and event id into the file URL', () => {
    const [img] = ctx._imagesFor('GH/662', '2026 0918',
      EV({ id: 'a b', files: ['wide.jpg'] }));
    expect(img.url).toBe(
      '/api/archive/file/GH%2F662/2026%200918/spectro/a%20b__wide.jpg');
  });
});

describe('_matchesFilter', () => {
  it('keeps everything on "all"', () => {
    expect(['none', 'transient', 'spectrum']
      .every(state => ctx._matchesFilter(EV({ state })))).toBe(true);
  });

  it('"detected" keeps transients and spectra, drops empty events', () => {
    ctx._spectroState.filter = 'detected';
    expect(ctx._matchesFilter(EV({ state: 'transient' }))).toBe(true);
    expect(ctx._matchesFilter(EV({ state: 'spectrum' }))).toBe(true);
    expect(ctx._matchesFilter(EV({ state: 'none' }))).toBe(false);
  });

  it('"spectrum" keeps only events that produced one', () => {
    ctx._spectroState.filter = 'spectrum';
    expect(ctx._matchesFilter(EV({ state: 'spectrum' }))).toBe(true);
    expect(ctx._matchesFilter(EV({ state: 'transient' }))).toBe(false);
  });
});

describe('mkEventCard', () => {
  it('numbers its thumbnails from the offset it was given', () => {
    const html = ctx.mkEventCard('GH662S', '20260918',
      EV({ files: ['wide.jpg', 'spectro.jpg'] }), 7);
    expect(html).toContain('spectroLightbox(7)');
    expect(html).toContain('spectroLightbox(8)');
  });

  it('marks the card with its reduction state', () => {
    expect(ctx.mkEventCard('C', '20260918', EV({ state: 'spectrum' }), 0))
      .toContain('spectro-state-spectrum');
    expect(ctx.mkEventCard('C', '20260918', EV({ state: 'weird' }), 0))
      .toContain('spectro-state-none');
  });

  it('escapes station-authored text rather than rendering it as markup', () => {
    const html = ctx.mkEventCard('C', '20260918',
      EV({ id: '<img src=x onerror=alert(1)>', source: '<b>x</b>' }), 0);
    expect(html).not.toContain('<img src=x');
    expect(html).toContain('&lt;img src=x');
    expect(html).not.toContain('<b>x</b>');
  });

  it('wires a one-shot retry onto every thumbnail', () => {
    const html = ctx.mkEventCard('GH662S', '20260918',
      EV({ files: ['wide.jpg', 'spectrum.png'] }), 0);
    expect(html.match(/onerror="spectroImgRetry\(this\)"/g)).toHaveLength(2);
  });

  it('offers a CSV link only when the spectrum was written', () => {
    expect(ctx.mkEventCard('C', '20260918', EV(), 0)).not.toContain('>CSV<');
    expect(ctx.mkEventCard('C', '20260918',
      EV({ files: ['spectrum.csv'] }), 0)).toContain('>CSV<');
  });
});

describe('mkSummary', () => {
  it('renders zeroes rather than NaN for a night with no counts', () => {
    const html = ctx.mkSummary({ date: '20260918' });
    expect(html).not.toContain('NaN');
    expect(html).toContain('<b>0</b>');
  });

  it('coerces a non-numeric count instead of interpolating it', () => {
    expect(ctx.mkSummary({ counts: { events: '3<script>' } }))
      .not.toContain('<script>');
  });
});

describe('renderSpectro', () => {
  const payload = {
    spectro: true, partial: false,
    cameras: [{
      cam: 'GH662S',
      nights: [{
        date: '20260918',
        counts: { events: 2, rms: 1, widefield: 1, detected: 1, spectra: 1 },
        events: [
          EV({ id: 'RO000A_20260918_212418', state: 'none',
               files: ['spectro.jpg', 'event.json'] }),
          EV({ id: 'WIDE_20260918_235813', trigger: 'widefield', state: 'spectrum',
               files: ['wide.jpg', 'spectro.jpg', 'spectrum.png', 'spectrum.csv'] }),
        ],
      }],
    }],
  };

  beforeEach(() => {
    Object.assign(ctx._spectroState, {
      host: 'gmnro01', data: JSON.parse(JSON.stringify(payload)),
      night: '20260918', cam: 'GH662S', filter: 'all', loading: false,
    });
  });

  it('builds one lightbox entry per image, in card order', () => {
    ctx.renderSpectro('gmnro01');
    expect(ctx._lightbox.items.map(i => i.kind))
      .toEqual(['spectro', 'wide', 'spectro', 'spectrum']);
    expect(ctx._lightbox.items[3].id).toBe('WIDE_20260918_235813');
  });

  it('rebuilds the lightbox list when a filter hides events', () => {
    ctx._spectroState.filter = 'spectrum';
    ctx.renderSpectro('gmnro01');
    expect(ctx._lightbox.items).toHaveLength(3);
    expect(ctx._lightbox.items.every(i => i.id === 'WIDE_20260918_235813')).toBe(true);
  });

  it('says so when a filter matches nothing, instead of rendering an empty grid', () => {
    ctx._spectroState.data.cameras[0].nights[0].events =
      [EV({ state: 'none', files: [] })];
    ctx._spectroState.filter = 'spectrum';
    ctx.renderSpectro('gmnro01');
    expect(document.getElementById('pane-spectro').innerHTML)
      .toContain('No events match this filter');
  });

  it('surfaces a failed listing as an error with a retry, not as "no events"', () => {
    ctx._spectroState.data = { spectro: true, failed: true, cameras: [] };
    ctx.renderSpectro('gmnro01');
    const html = document.getElementById('pane-spectro').innerHTML;
    expect(html).toContain('Could not list');
    expect(html).toContain('spectroReload');
  });

  it('warns when the payload is partial so a short list is not read as complete', () => {
    ctx._spectroState.data.partial = true;
    ctx.renderSpectro('gmnro01');
    expect(document.getElementById('pane-spectro').innerHTML)
      .toContain('could not be read');
  });

  it('tells the operator the pipeline has not run yet when a camera has no nights', () => {
    ctx._spectroState.data.cameras = [{ cam: 'GH662S', nights: [] }];
    ctx.renderSpectro('gmnro01');
    expect(document.getElementById('pane-spectro').innerHTML)
      .toContain('No events yet');
  });
});

// A storagebox read that gets shed answers 503, and the <img> that took one
// stays broken until the page is reloaded. On gmnro01/20260920 the Spectro tab
// asks for 170 pictures at once and 66 of them came back 503 (measured against
// prod, 2026-09-22), so a third of the gallery rendered empty and clicking a
// plot opened the lightbox on a broken image. The route no longer sheds like
// that, but the shed itself is deliberate and will still fire, so a failed
// picture has to heal itself once.
describe('spectroImgRetry', () => {
  const mkImg = (src = '/api/archive/file/GH662S/20260920/spectro/a__spectrum.png') => {
    const img = document.createElement('img');
    img.setAttribute('src', src);
    return img;
  };

  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it('re-requests the picture once, after a pause, with a cache-buster', () => {
    const img = mkImg();
    ctx.spectroImgRetry(img);
    expect(img.getAttribute('src')).not.toContain('retry=1');   // not immediate
    vi.runAllTimers();
    expect(img.getAttribute('src'))
      .toBe('/api/archive/file/GH662S/20260920/spectro/a__spectrum.png?retry=1');
  });

  it('never retries twice, so a genuinely missing file is not a request loop', () => {
    const img = mkImg();
    ctx.spectroImgRetry(img);
    vi.runAllTimers();
    const afterFirst = img.getAttribute('src');

    ctx.spectroImgRetry(img);          // the retry's own load failed too
    vi.runAllTimers();
    expect(img.getAttribute('src')).toBe(afterFirst);
  });

  it('appends to a URL that already carries a query instead of breaking it', () => {
    const img = mkImg('/api/archive/file/C/20260920/spectro/a__wide.jpg?download=1');
    ctx.spectroImgRetry(img);
    vi.runAllTimers();
    expect(img.getAttribute('src'))
      .toBe('/api/archive/file/C/20260920/spectro/a__wide.jpg?download=1&retry=1');
  });

  it('ignores an element with no src rather than requesting the page itself', () => {
    const img = document.createElement('img');
    expect(() => ctx.spectroImgRetry(img)).not.toThrow();
    vi.runAllTimers();
    expect(img.getAttribute('src')).toBe(null);
  });
});

describe('spectroLoad', () => {
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

  it('retries once after a gateway timeout and renders the listing', async () => {
    vi.useFakeTimers();
    const ok = { spectro: true, partial: false, cameras: [{ cam: 'GH662S', nights: [] }] };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: false, status: 504 })
      .mockResolvedValueOnce({ ok: true, json: async () => ok });
    vi.stubGlobal('fetch', fetchMock);
    ctx._spectroState.host = 'gmnro01'; ctx._spectroState.data = null; ctx._spectroState.loading = false;
    const p = ctx.spectroLoad('gmnro01');
    await vi.advanceTimersByTimeAsync(ctx.SPECTRO_RETRY_MS);
    await p;
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(ctx._spectroState.data.failed).toBeUndefined();
  });

  it('reports failure only after the retry also fails', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 504 });
    vi.stubGlobal('fetch', fetchMock);
    ctx._spectroState.host = 'gmnro01'; ctx._spectroState.data = null; ctx._spectroState.loading = false;
    const p = ctx.spectroLoad('gmnro01');
    await vi.advanceTimersByTimeAsync(ctx.SPECTRO_RETRY_MS);
    await p;
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(ctx._spectroState.data.failed).toBe(true);
    expect(document.getElementById('pane-spectro').textContent).toContain('Could not list');
  });
});
