import { describe, it, expect, vi, beforeEach } from 'vitest';

// ── Hidden detections (dashboard-hidden.js) ───────────────────────────────────
//
// A detection hidden as a false positive leaves the lists; "Show hidden"
// (remembered in localStorage) brings it back marked; one click hides, with a
// "Hidden. Undo" notice. The module reads the session from state, so each
// test imports a fresh copy and sets state.AUTH_USER / CAN_HIDE.

let fetchStub = null;

function stubFetch(reply) {
  const calls = [];
  vi.stubGlobal('fetch', fetchStub = vi.fn(async (url, opts = {}) => {
    calls.push({ url, method: opts.method || 'GET', body: opts.body ? JSON.parse(opts.body) : null });
    const r = typeof reply === 'function' ? reply(url, opts) : reply;
    return { ok: r.status === 200, status: r.status, json: async () => r.body };
  }));
  return calls;
}

async function importFresh({ user = 'operator', canHide = true } = {}) {
  vi.resetModules();
  const common = await import('../../dashboard/static/dashboard-common.js');
  common.state.AUTH_USER = user;
  common.state.CAN_HIDE = canHide;
  return import('../../dashboard/static/dashboard-hidden.js');
}

const BIRD = { cam: 'RO0017', meteor_time: '2026-11-05T21:20:05.400', rms: { mag_apparent: -3.5 } };
const REAL = { cam: 'RO0017', meteor_time: '2026-11-05T22:30:10.100', rms: { mag_apparent: 1.2 } };
const HIDE_RESP = {
  cam: 'RO0017', ff_file: 'FF_RO0017_20261105_212000_123_0012345.fits', meteor_no: 1,
  time_utc: '2026-11-05 21:20:05.4', hidden: true, hidden_by: 'operator',
  hidden_at: '2026-11-22T10:00:00+00:00', hidden_reason: 'bird', changed: true,
};

beforeEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
  document.body.innerHTML = '';
});

describe('the Show hidden toggle', () => {
  it('is remembered per browser and adds show_hidden=1 for signed-in users', async () => {
    const h = await importFresh();
    expect(h.getShowHidden()).toBe(false);
    expect(h.withHidden('/api/detections/20261105')).toBe('/api/detections/20261105');
    h.setShowHidden(true);
    expect(localStorage.getItem(h.SHOW_HIDDEN_KEY)).toBe('1');
    const again = await importFresh();
    expect(again.getShowHidden()).toBe(true);
    expect(again.withHidden('/api/x?a=1')).toBe('/api/x?a=1&show_hidden=1');
    again.setShowHidden(false);
    expect(localStorage.getItem(again.SHOW_HIDDEN_KEY)).toBeNull();
  });

  it('never asks for hidden detections for an anonymous visitor, and renders no toggle', async () => {
    localStorage.setItem('rovimen_show_hidden', '1');
    const h = await importFresh({ user: null, canHide: false });
    expect(h.showingHidden()).toBe(false);
    expect(h.withHidden('/api/detections/20261105')).toBe('/api/detections/20261105');
    expect(h.toggleHtml('t')).toBe('');
  });

  it('labels the count and flips aria-pressed on click', async () => {
    const h = await importFresh();
    document.body.innerHTML = h.toggleHtml('t', 3);
    const btn = document.getElementById('t');
    expect(btn.textContent.trim()).toBe('Show hidden (3)');
    expect(btn.getAttribute('aria-pressed')).toBe('false');
    const seen = [];
    h.wireToggle(btn, on => seen.push(on));
    btn.click();
    expect(seen).toEqual([true]);
    expect(btn.getAttribute('aria-pressed')).toBe('true');
    expect(h.getShowHidden()).toBe(true);
    h.updateToggle(btn, 0);
    expect(btn.textContent).toBe('Show hidden');
  });

  it('is absent for a signed-in session that may not hide (magic link), which never lists hidden', async () => {
    localStorage.setItem('rovimen_show_hidden', '1');
    const h = await importFresh({ user: 'magic', canHide: false });
    expect(h.toggleHtml('t', 2)).toBe('');
    expect(h.showingHidden()).toBe(false);
    expect(h.withHidden('/api/archive/nights_full/RO0017')).toBe('/api/archive/nights_full/RO0017');
  });

  it("takes the page's own toolbar-button class and keeps every toggle on the page in step", async () => {
    const h = await importFresh();
    document.body.innerHTML = h.toggleHtml('a', 0, '', 'grid-btn') + h.toggleHtml('b', 4, 'dt-tall');
    const a = document.getElementById('a'), b = document.getElementById('b');
    expect(a.className).toBe('grid-btn');
    expect(b.className).toBe('hd-toggle dt-tall');
    expect(b.textContent.trim()).toBe('Show hidden (4)');
    const seen = [];
    h.wireToggle(a, on => seen.push(on));
    h.wireToggle(b, on => seen.push(`b${on}`));
    a.click();
    expect([a, b].map(x => x.getAttribute('aria-pressed'))).toEqual(['true', 'true']);
    b.click();
    expect([a, b].map(x => x.getAttribute('aria-pressed'))).toEqual(['false', 'false']);
    expect(seen).toEqual([true, 'bfalse']);
  });

  it('mounts into a toolbar slot, which stays hidden for a session without the toggle', async () => {
    let h = await importFresh({ user: null, canHide: false });
    document.body.innerHTML = '<div id="slot" hidden></div>';
    const slot = document.getElementById('slot');
    expect(h.mountToggle(slot, 'sf-hidden-toggle', () => {})).toBeNull();
    expect(slot.hidden).toBe(true);
    expect(slot.innerHTML).toBe('');

    h = await importFresh();
    const flips = [];
    const btn = h.mountToggle(slot, 'sf-hidden-toggle', on => flips.push(on), { count: 3, baseClass: 'grid-btn' });
    expect(slot.hidden).toBe(false);
    expect(btn.id).toBe('sf-hidden-toggle');
    expect(btn.textContent.trim()).toBe('Show hidden (3)');
    btn.click();
    expect(flips).toEqual([true]);
    expect(h.getShowHidden()).toBe(true);
    expect(h.mountToggle(null, 'x', () => {})).toBeNull();
  });
});

describe('filtering and marking', () => {
  it('names a detection by key when it has one, else by camera and second', async () => {
    const h = await importFresh();
    expect(h.hideTarget(BIRD)).toEqual({ cam: 'RO0017', time_utc: '2026-11-05T21:20:05' });
    expect(h.hideTarget({ ff_file: 'FF_x.fits', meteor_no: 2, time_utc: '2026-11-05 21:20:05' }, 'ro0017'))
      .toEqual({ cam: 'RO0017', ff_file: 'FF_x.fits', meteor_no: 2 });
    expect(h.hideTarget({ camera: 'RO000M', time_utc: '2026-11-05 21:20:05.9' }))
      .toEqual({ cam: 'RO000M', time_utc: '2026-11-05T21:20:05' });
    expect(h.hideTarget({ cam: 'RO0017' })).toBeNull();
  });

  it('marks every matching clip of a night payload and leaves it out of the list', async () => {
    const h = await importFresh();
    const bird = { ...BIRD }, real = { ...REAL };
    const witnessBird = { ...BIRD, host_key: 'a' };
    const other = { cam: 'RO000M', meteor_time: '2026-11-05T21:20:05.900', host_key: 'b' };
    const payload = {
      by_station: { a: { cameras: { RO0017: [bird, real] } } },
      detections: [{ ...BIRD }, { ...REAL }],
      events: [{ witnesses: [witnessBird, other], witness_count: 2 }],
    };
    expect(h.markInPayload(payload, HIDE_RESP)).toBe(3);
    expect(bird).toMatchObject({ hidden: true, hidden_by: 'operator', hidden_reason: 'bird' });
    expect(real.hidden).toBeUndefined();
    expect(h.visibleItems([bird, real], false)).toEqual([real]);
    expect(h.visibleItems([bird, real], true)).toEqual([bird, real]);
    // Another camera at the same second is a different detection.
    expect(other.hidden).toBeUndefined();
  });

  it('applies the event rule: all hidden = hidden, some hidden = stays with the others', async () => {
    const h = await importFresh();
    const a = { ...BIRD, hidden: true }, b = { cam: 'RO000M', meteor_time: BIRD.meteor_time };
    const some = h.eventListing({ witnesses: [a, b] }, false);
    expect(some).toMatchObject({ listed: true, total: 2, allHidden: false });
    expect(some.witnesses).toEqual([b]);
    const all = h.eventListing({ witnesses: [a, { ...b, hidden: true }] }, false);
    expect(all.listed).toBe(false);
    expect(h.eventListing({ witnesses: [a, { ...b, hidden: true }] }, true))
      .toMatchObject({ listed: true, allHidden: true });
    // Witnesses the server already left out still count toward "multi-station".
    expect(h.eventListing({ witnesses: [b], hidden_witnesses: 1 }, false).total).toBe(2);
  });

  it('clears the hide fields on an unhide', async () => {
    const h = await importFresh();
    const item = { ...BIRD };
    h.applyHidden(item, HIDE_RESP);
    expect(h.hiddenNoteText(item)).toBe('Hidden by operator, 2026-11-22 (bird)');
    h.applyHidden(item, { ...HIDE_RESP, hidden: false });
    expect('hidden' in item).toBe(false);
    expect(h.hiddenNoteText(item)).toBe('');
  });

  it('shows Hide / Unhide only to sessions that may hide', async () => {
    let h = await importFresh();
    expect(h.hideButtonHtml({})).toContain('>Hide</button>');
    expect(h.hideButtonHtml({ hidden: true })).toContain('>Unhide</button>');
    h = await importFresh({ user: 'magic', canHide: false });
    expect(h.hideButtonHtml({})).toBe('');
  });
});

describe('one-click hide with undo', () => {
  it('posts the hide, updates the page, and Undo posts the unhide', async () => {
    const h = await importFresh();
    const calls = stubFetch((url) => ({
      status: 200,
      body: url.endsWith('/hide') ? HIDE_RESP : { ...HIDE_RESP, hidden: false },
    }));
    const applied = [];
    const resp = await h.toggleHide(h.hideTarget(BIRD), true, r => applied.push(r.hidden), { page: 'detections' });
    expect(resp.hidden).toBe(true);
    expect(calls[0]).toMatchObject({ url: '/api/detections/hide', method: 'POST',
      body: { cam: 'RO0017', time_utc: '2026-11-05T21:20:05', page: 'detections' } });
    const notice = document.querySelector('.hd-undo');
    expect(notice.textContent).toContain('Hidden.');
    notice.querySelector('button').click();
    await vi.waitFor(() => expect(applied).toEqual([true, false]));
    expect(calls[1].url).toBe('/api/detections/unhide');
    expect(document.querySelector('.hd-undo')).toBeNull();
    // Lists fetched after a change bypass the browser cache.
    expect(h.withHidden('/api/detections/20261105')).toMatch(/[?&]hv=2$/);
  });

  it('reports a refused hide and changes nothing', async () => {
    const h = await importFresh();
    stubFetch({ status: 429, body: {} });
    const applied = [];
    expect(await h.toggleHide(h.hideTarget(BIRD), true, r => applied.push(r))).toBeNull();
    expect(applied).toEqual([]);
    expect(document.querySelector('.hd-undo')).toBeNull();
    expect(document.querySelector('.toast').textContent).toMatch(/Too many hides/);
  });

  it('wires the video modal button, hidden when there is no target', async () => {
    const h = await importFresh();
    stubFetch({ status: 200, body: HIDE_RESP });
    const btn = document.createElement('button');
    document.body.appendChild(btn);
    h.wireHideButton(btn, null);
    expect(btn.style.display).toBe('none');
    const changes = [];
    h.wireHideButton(btn, { target: h.hideTarget(BIRD), hidden: false, onChange: r => changes.push(r.hidden) });
    expect(btn.style.display).toBe('');
    expect(btn.textContent).toBe('Hide');
    btn.click();
    await vi.waitFor(() => expect(changes).toEqual([true]));
    expect(btn.textContent).toBe('Unhide');
  });
});
