import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

import * as ctx from '../../dashboard/static/dashboard-common.js';

describe('fmtBytes', () => {
  const f = ctx.fmtBytes;

  it('formats 0 bytes', () => {
    expect(f(0)).toBe('0.0 B');
  });
  it('formats bytes under 1 KB', () => {
    expect(f(512)).toBe('512.0 B');
  });
  it('formats exact 1 KB', () => {
    expect(f(1024)).toBe('1.0 KB');
  });
  it('formats MB', () => {
    expect(f(1048576)).toBe('1.0 MB');
  });
  it('formats GB', () => {
    expect(f(1073741824)).toBe('1.0 GB');
  });
  it('formats TB', () => {
    expect(f(1099511627776)).toBe('1.0 TB');
  });
  it('shows decimal precision', () => {
    expect(f(1536)).toBe('1.5 KB');
  });
  it('returns dash for null', () => {
    expect(f(null)).toBe('—');
  });
  it('returns dash for undefined', () => {
    expect(f(undefined)).toBe('—');
  });
});

describe('fmtDate', () => {
  const f = ctx.fmtDate;

  it('formats YYYYMMDD to YYYY-MM-DD', () => {
    expect(f('20260602')).toBe('2026-06-02');
  });
  it('returns dash for empty string', () => {
    expect(f('')).toBe('—');
  });
  it('returns dash for null', () => {
    expect(f(null)).toBe('—');
  });
  it('returns dash for undefined', () => {
    expect(f(undefined)).toBe('—');
  });
});

describe('parseFn', () => {
  const f = ctx.parseFn;

  it('parses RMS filename with timestamp', () => {
    expect(f('FF_RO000T_20260530_214500_123_0000128.fits')).toBe('2026-05-30 21:45:00 UTC');
  });
  it('returns empty for non-matching filename', () => {
    expect(f('random.txt')).toBe('');
  });
  it('handles filename with extra underscores', () => {
    expect(f('some_prefix_20260101_120000_suffix')).toBe('2026-01-01 12:00:00 UTC');
  });
});

describe('barColor', () => {
  const f = ctx.barColor;

  it('returns green for low usage', () => {
    expect(f(50)).toBe('var(--green)');
  });
  it('returns yellow for moderate usage', () => {
    expect(f(75)).toBe('var(--yellow)');
  });
  it('returns red for high usage', () => {
    expect(f(90)).toBe('var(--red)');
  });
  it('boundary: 70 is green', () => {
    expect(f(70)).toBe('var(--green)');
  });
  it('boundary: 71 is yellow', () => {
    expect(f(71)).toBe('var(--yellow)');
  });
  it('boundary: 85 is yellow', () => {
    expect(f(85)).toBe('var(--yellow)');
  });
  it('boundary: 86 is red', () => {
    expect(f(86)).toBe('var(--red)');
  });
});

describe('escHtml', () => {
  const f = ctx.escHtml;

  it('escapes ampersand', () => {
    expect(f('a&b')).toBe('a&amp;b');
  });
  it('escapes less-than', () => {
    expect(f('<script>')).toBe('&lt;script&gt;');
  });
  it('escapes double quotes', () => {
    expect(f('"hello"')).toBe('&quot;hello&quot;');
  });
  it('escapes single quotes', () => {
    expect(f("it's")).toBe("it&#39;s");
  });
  it('returns empty for null', () => {
    expect(f(null)).toBe('');
  });
  it('returns empty for undefined', () => {
    expect(f(undefined)).toBe('');
  });
  it('handles string with all special chars', () => {
    expect(f('<a href="x">&\'</a>')).toBe('&lt;a href=&quot;x&quot;&gt;&amp;&#39;&lt;/a&gt;');
  });
  it('passes through clean strings unchanged', () => {
    expect(f('hello world')).toBe('hello world');
  });

  // XSS: station-controlled filename / label payloads must be neutralised.
  it('neutralises single-quote in filename (breaks inline onclick)', () => {
    const filename = "20260601_foo'onclick='alert(1)'.mp4";
    const escaped = f(filename);
    // Must not contain a raw single quote that could terminate an attribute
    expect(escaped).not.toContain("'");
    // Must contain the safe entity instead
    expect(escaped).toContain('&#39;');
  });
  it('neutralises img onerror XSS payload in filename', () => {
    const filename = '<img src=x onerror=alert(1)>.mp4';
    const escaped = f(filename);
    // The opening tag must be entity-encoded so the browser never parses it
    // as a tag; onerror= may still appear as text, but cannot execute.
    expect(escaped).not.toContain('<img');
    expect(escaped).toContain('&lt;img');
    // Closing > is also encoded, completing the tag-break
    expect(escaped).not.toContain('>');
  });
  it('neutralises script injection in station label', () => {
    const label = '<script>alert(1)</script>';
    const escaped = f(label);
    expect(escaped).not.toContain('<script>');
    expect(escaped).toContain('&lt;script&gt;');
  });
});

// ── XSS: YouTube URL allow-list (mirrors compCartShowList safeYtUrl logic) ────
// Any URL that is not https: must be rejected to block javascript:/data: hrefs.
describe('safeYtUrl-equivalent URL validation', () => {
  function safeYtUrl(url) {
    if (!url) return null;
    try {
      const u = new URL(url);
      return u.protocol === 'https:' ? url : null;
    } catch { return null; }
  }

  it('accepts a valid https YouTube URL', () => {
    expect(safeYtUrl('https://www.youtube.com/watch?v=abc123')).toBe('https://www.youtube.com/watch?v=abc123');
  });
  it('rejects a javascript: URL', () => {
    expect(safeYtUrl('javascript:alert(1)')).toBeNull();
  });
  it('rejects a data: URL', () => {
    expect(safeYtUrl('data:text/html,<script>alert(1)</script>')).toBeNull();
  });
  it('rejects an http: URL (non-https)', () => {
    expect(safeYtUrl('http://example.com/video')).toBeNull();
  });
  it('rejects null', () => {
    expect(safeYtUrl(null)).toBeNull();
  });
  it('rejects empty string', () => {
    expect(safeYtUrl('')).toBeNull();
  });
  it('rejects a non-URL string', () => {
    expect(safeYtUrl('not a url')).toBeNull();
  });
});

describe('fetchJson', () => {
  const { fetchJson } = ctx;

  beforeEach(() => {
    vi.restoreAllMocks();
    // Reset location.href spy if set
    delete window.__fetchJsonRedirect;
  });

  function mockFetch({ status = 200, ok = true, contentType = 'application/json', body = '{"ok":true}' } = {}) {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      status,
      ok,
      headers: { get: (h) => h === 'content-type' ? contentType : null },
      json: () => Promise.resolve(JSON.parse(body)),
    }));
  }

  it('returns parsed JSON for a 200 application/json response', async () => {
    mockFetch({ body: '{"value":42}' });
    const result = await fetchJson('/api/test');
    expect(result).toEqual({ value: 42 });
  });

  it('passes opts to fetch', async () => {
    mockFetch({ body: '{"saved":true}' });
    await fetchJson('/api/save', { method: 'POST' });
    expect(fetch).toHaveBeenCalledWith('/api/save', { method: 'POST' });
  });

  it('throws on non-ok response (500)', async () => {
    mockFetch({ status: 500, ok: false, contentType: 'text/html', body: '{}' });
    await expect(fetchJson('/api/fail')).rejects.toThrow('HTTP 500');
  });

  it('throws on non-ok response (403)', async () => {
    mockFetch({ status: 403, ok: false, contentType: 'application/json', body: '{"error":"forbidden"}' });
    await expect(fetchJson('/api/private')).rejects.toThrow('HTTP 403');
  });

  it('throws with correct status property on non-ok', async () => {
    mockFetch({ status: 503, ok: false, contentType: 'text/html', body: '{}' });
    const err = await fetchJson('/api/down').catch(e => e);
    expect(err.status).toBe(503);
  });

  it('on 401 with a live session: redirects to /login and throws "Session expired"', async () => {
    // A logged-in visitor whose cookie expired mid-session must be bounced.
    const loc = { href: '', assign: vi.fn() };
    Object.defineProperty(window, 'location', { value: loc, writable: true });
    ctx.state.AUTH_USER = 'operator';           // simulate a live session
    mockFetch({ status: 401, ok: false, contentType: 'text/html', body: '{}' });
    const err = await fetchJson('/api/protected').catch(e => e);
    expect(err).toBeInstanceOf(Error);
    expect(err.status).toBe(401);
    expect(err.message).toBe('Session expired');
    expect(loc.href).toBe('/login');
    ctx.state.AUTH_USER = null;              // reset for other tests
  });

  it('on 401 with NO session (anon public page): does NOT redirect, throws "Unauthorized"', async () => {
    // Anonymous visitors on a public page (e.g. /events, whose curated
    // detection feed stays login-gated) must NOT be ejected to /login when a
    // gated background fetch 401s — the caller degrades gracefully instead.
    const loc = { href: '', assign: vi.fn() };
    Object.defineProperty(window, 'location', { value: loc, writable: true });
    ctx.state.AUTH_USER = null;              // anonymous
    mockFetch({ status: 401, ok: false, contentType: 'text/html', body: '{}' });
    const err = await fetchJson('/api/detections/nights').catch(e => e);
    expect(err).toBeInstanceOf(Error);
    expect(err.status).toBe(401);
    expect(err.message).toBe('Unauthorized');
    expect(loc.href).toBe('');               // no redirect
  });

  it('throws on ok response with non-JSON content-type', async () => {
    mockFetch({ status: 200, ok: true, contentType: 'text/html', body: '{}' });
    await expect(fetchJson('/api/oops')).rejects.toThrow('Not JSON');
  });

  it('accepts text/json content-type', async () => {
    mockFetch({ status: 200, ok: true, contentType: 'text/json', body: '{"x":1}' });
    const result = await fetchJson('/api/alt');
    expect(result).toEqual({ x: 1 });
  });
});

describe('fetchJsonResponse', () => {
  const { fetchJsonResponse } = ctx;

  beforeEach(() => {
    vi.restoreAllMocks();
    ctx.state.AUTH_USER = null;
  });

  // Same shape as the fetchJson mock, plus arbitrary response headers so the
  // header-reading callers (e.g. the overview stacks warming flag) can be
  // exercised.
  function mockFetch({ status = 200, ok = true, contentType = 'application/json',
                       body = '{"ok":true}', headers = {} } = {}) {
    const all = { 'content-type': contentType, ...headers };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      status,
      ok,
      headers: { get: (h) => (h.toLowerCase() in all ? all[h.toLowerCase()] : null) },
      json: () => Promise.resolve(JSON.parse(body)),
    }));
  }

  it('hands back the validated Response so callers can read headers', async () => {
    mockFetch({ body: '[]', headers: { 'x-stacks-warming': '1' } });
    const res = await fetchJsonResponse('/api/overview/stacks');
    expect(res.headers.get('X-Stacks-Warming')).toBe('1');
    expect(await res.json()).toEqual([]);
  });

  it('returns null for a header the response does not carry', async () => {
    mockFetch({ body: '[]' });
    const res = await fetchJsonResponse('/api/overview/stacks');
    expect(res.headers.get('X-Stacks-Warming')).toBeNull();
  });

  // The guards below are the reason fetchJson delegates here rather than
  // duplicating them: any drift would silently un-protect one of the two.
  it('applies the same non-ok guard as fetchJson', async () => {
    mockFetch({ status: 500, ok: false, contentType: 'text/html', body: '{}' });
    const err = await fetchJsonResponse('/api/fail').catch(e => e);
    expect(err.message).toBe('HTTP 500');
    expect(err.status).toBe(500);
  });

  it('applies the same content-type guard as fetchJson', async () => {
    mockFetch({ status: 200, ok: true, contentType: 'text/html', body: '{}' });
    await expect(fetchJsonResponse('/api/oops')).rejects.toThrow('Not JSON');
  });

  it('applies the same 401 handling as fetchJson', async () => {
    const loc = { href: '', assign: vi.fn() };
    Object.defineProperty(window, 'location', { value: loc, writable: true });
    ctx.state.AUTH_USER = 'operator';
    mockFetch({ status: 401, ok: false, contentType: 'text/html', body: '{}' });
    const err = await fetchJsonResponse('/api/protected').catch(e => e);
    expect(err.message).toBe('Session expired');
    expect(loc.href).toBe('/login');
    ctx.state.AUTH_USER = null;
  });
});

describe('wireLockButton', () => {
  const mkBtn = () => document.createElement('button');
  const base = {
    host: 'gmnro12', camera: 'RO0015', date: '20260710',
    filename: 'RO0015_20260710_223000_123.mkv',
  };

  beforeEach(() => {
    ctx.state.USER_ROLE = 'admin';       // fleet-wide lock rights
    ctx.state.USER_STATIONS = [];
    ctx._resetRoleCacheForTests();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('hides when the clip lacks a full identity', async () => {
    const btn = mkBtn();
    await ctx.wireLockButton(btn, { ...base, filename: '', locked: false });
    expect(btn.style.display).toBe('none');
  });

  it('hides for a guest session (the server 401s its POST)', async () => {
    ctx.state.USER_ROLE = 'guest';
    stubAuthStatus({ role: 'guest' });
    const btn = mkBtn();
    await ctx.wireLockButton(btn, { ...base, locked: false });
    expect(btn.style.display).toBe('none');
  });

  it('renders a disabled marker for an already-locked clip', async () => {
    const btn = mkBtn();
    await ctx.wireLockButton(btn, { ...base, locked: true, lockType: 'detection' });
    expect(btn.style.display).toBe('');
    expect(btn.disabled).toBe(true);
    expect(btn.textContent).toContain('Locked');
    expect(btn.onclick).toBeNull();
  });

  it('renders a clickable Lock button for an unlocked .mkv clip', async () => {
    const btn = mkBtn();
    await ctx.wireLockButton(btn, { ...base, locked: false });
    expect(btn.style.display).toBe('');
    expect(btn.disabled).toBe(false);
    expect(btn.textContent).toContain('Lock clip');
    expect(typeof btn.onclick).toBe('function');
  });

  it('hides for an unlocked non-.mkv view (nothing lockable)', async () => {
    const btn = mkBtn();
    await ctx.wireLockButton(btn, {
      ...base, filename: 'RO0015_20260710_223000.mp4', locked: false,
    });
    expect(btn.style.display).toBe('none');
  });

  it('grants a host operator on a station they do not own', async () => {
    ctx.state.USER_ROLE = 'host';
    ctx.state.USER_STATIONS = ['gmnro11'];   // base.host is gmnro12
    const btn = mkBtn();
    await ctx.wireLockButton(btn, { ...base, locked: false });
    expect(btn.style.display).toBe('');
  });

  it.each(['admin', 'host', 'visitor', 'press'])(
    'grants %s fleet-wide lock rights with no station assignments', async (role) => {
      ctx.state.USER_ROLE = role;
      ctx.state.USER_STATIONS = [];
      const btn = mkBtn();
      await ctx.wireLockButton(btn, { ...base, locked: false });
      expect(btn.style.display).toBe('');
      expect(btn.disabled).toBe(false);
    });

  it('resolves the role via /api/auth/status on pages that never set it (Events)', async () => {
    ctx.state.USER_ROLE = 'guest';       // events/highlights never assign USER_ROLE
    stubAuthStatus({ user: 'operator', role: 'admin', stations: [] });
    const btn = mkBtn();
    await ctx.wireLockButton(btn, { ...base, locked: true, lockType: 'detection' });
    expect(btn.style.display).toBe('');
    expect(btn.textContent).toContain('Locked');
    expect(ctx.state.USER_ROLE).toBe('admin');   // written back for other consumers
  });
});

/** Stub fetch for the /api/auth/status role fallback. */
function stubAuthStatus(body) {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  }));
}

describe('wireProcessClipButton', () => {
  const mkBtn = () => document.createElement('button');
  const base = {
    host: 'gmnro10', camera: 'RO000G', date: '20260731',
    filename: 'RO000G_20260731_223000_color.mkv',
  };

  function mockFetch({ status = 200, ok = true, body = '{"ok":true,"locked":true,"lock_type":"manual"}' } = {}) {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      status,
      ok,
      headers: { get: () => 'application/json' },
      json: () => Promise.resolve(JSON.parse(body)),
      text: () => Promise.resolve(body),
    }));
  }

  beforeEach(() => {
    ctx.state.USER_ROLE = 'admin';
    ctx._resetRoleCacheForTests();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('renders a clickable Process clip button for an admin', async () => {
    const btn = mkBtn();
    await ctx.wireProcessClipButton(btn, base);
    expect(btn.style.display).toBe('');
    expect(btn.disabled).toBe(false);
    expect(btn.textContent).toBe('Process clip');
    expect(typeof btn.onclick).toBe('function');
  });

  it('hides when the clip lacks a full identity', async () => {
    const btn = mkBtn();
    await ctx.wireProcessClipButton(btn, { ...base, filename: '' });
    expect(btn.style.display).toBe('none');
  });

  it.each(['host', 'visitor', 'press', 'guest'])(
    'hides for a %s session (admin-only control)', async (role) => {
      ctx.state.USER_ROLE = role;
      stubAuthStatus({ role });
      const btn = mkBtn();
      await ctx.wireProcessClipButton(btn, base);
      expect(btn.style.display).toBe('none');
    });

  it('hides for a non-.mkv view (nothing to deliver)', async () => {
    const btn = mkBtn();
    await ctx.wireProcessClipButton(btn, {
      ...base, filename: 'RO000G_20260731_223000_color.mp4',
    });
    expect(btn.style.display).toBe('none');
  });

  it('resolves the role via /api/auth/status on pages that never set it (Events)', async () => {
    ctx.state.USER_ROLE = 'guest';       // events/highlights never assign USER_ROLE
    stubAuthStatus({ user: 'operator', role: 'admin', stations: [] });
    const btn = mkBtn();
    await ctx.wireProcessClipButton(btn, base);
    expect(btn.style.display).toBe('');
    expect(btn.textContent).toBe('Process clip');
  });

  it('renders a disabled Processed marker for a stitched boundary splice', async () => {
    const btn = mkBtn();
    await ctx.wireProcessClipButton(btn, {
      ...base, filename: 'MD0001_20260731_185555_185615_color.mkv',
    });
    expect(btn.style.display).toBe('');
    expect(btn.disabled).toBe(true);
    expect(btn.textContent).toBe('✓ Processed');
  });

  it('hides for an .mkv name the station chunk pattern would refuse', async () => {
    const btn = mkBtn();
    await ctx.wireProcessClipButton(btn, {
      ...base, filename: 'RO000G_20260731_oddname.mkv',
    });
    expect(btn.style.display).toBe('none');
  });

  it('POSTs to the clip-scoped proxy and reports Started', async () => {
    mockFetch();
    const btn = mkBtn();
    await ctx.wireProcessClipButton(btn, base);
    await btn.onclick();
    expect(fetch).toHaveBeenCalledWith(
      '/api/process-clip/gmnro10/RO000G/20260731/RO000G_20260731_223000_color.mkv',
      { method: 'POST' },
    );
    expect(btn.textContent).toBe('✓ Started');
    expect(btn.disabled).toBe(true);
    // The button restores itself so a second clip delivery stays possible.
    vi.runAllTimers();
    expect(btn.textContent).toBe('Process clip');
    expect(btn.disabled).toBe(false);
  });

  it('reports an in-flight delivery from a 202 response', async () => {
    mockFetch({ status: 202, ok: false, body: '{"ok":true,"running":true}' });
    const btn = mkBtn();
    await ctx.wireProcessClipButton(btn, base);
    await btn.onclick();
    expect(btn.textContent).toBe('✓ Already running');
  });

  it('surfaces a server error in the button without throwing', async () => {
    mockFetch({ status: 502, ok: false, body: 'station unreachable' });
    const btn = mkBtn();
    await ctx.wireProcessClipButton(btn, base);
    await btn.onclick();
    expect(btn.textContent).toContain('✗');
    vi.runAllTimers();
    expect(btn.disabled).toBe(false);
  });

  it('renders a disabled Processed marker for a clip already re-encoded', async () => {
    const btn = mkBtn();
    await ctx.wireProcessClipButton(btn, { ...base, processed: true });
    expect(btn.style.display).toBe('');
    expect(btn.disabled).toBe(true);
    expect(btn.textContent).toBe('✓ Processed');
    expect(btn.onclick).toBeNull();
  });

  it('turns into Processed, and stays disabled, when the server answers 409', async () => {
    mockFetch({ status: 409, ok: false, body: '{"ok":false,"processed":true}' });
    const btn = mkBtn();
    await ctx.wireProcessClipButton(btn, base);
    await btn.onclick();
    expect(btn.textContent).toBe('✓ Processed');
    vi.runAllTimers();
    expect(btn.disabled).toBe(true);
    expect(btn.textContent).toBe('✓ Processed');
  });
});

describe('fullResStackUrl', () => {
  it('maps the /stack/ thumbnail route to /fullstack/', () => {
    expect(ctx.fullResStackUrl('/stack/gmnro04/RO0014/20260924/RO0014_20260924_213311_stack.webp'))
      .toBe('/fullstack/gmnro04/RO0014/20260924/RO0014_20260924_213311_stack.webp');
  });
  it('leaves already full-res URLs and empty values alone', () => {
    const arch = '/api/archive/file/RO0014/20260924/meteors/x_stack.webp';
    expect(ctx.fullResStackUrl(arch)).toBe(arch);
    expect(ctx.fullResStackUrl(null)).toBeNull();
    expect(ctx.fullResStackUrl('')).toBe('');
  });
});

describe('downloadNameFor', () => {
  const f = ctx.downloadNameFor;
  const MKV = 'RO0017_20260709_214233_1_color.mkv';

  it('renames .mkv to .mp4 when the response is MP4', () => {
    expect(f(MKV, 'video/mp4')).toBe(
      'RO0017_20260709_214233_1_color.mp4');
  });

  it('keeps .mkv when the response is Matroska (archive fallback)', () => {
    expect(f(MKV, 'video/x-matroska')).toBe(MKV);
  });

  it('renames .mp4 back to .mkv when the response is Matroska', () => {
    expect(f('clip.mp4', 'video/x-matroska')).toBe('clip.mkv');
  });

  it('ignores charset and casing in the Content-Type', () => {
    expect(f(MKV, 'Video/MP4; charset=binary')).toBe(
      'RO0017_20260709_214233_1_color.mp4');
  });

  it('keeps the original name on an unknown Content-Type', () => {
    expect(f(MKV, 'application/octet-stream')).toBe(MKV);
    expect(f(MKV, 'text/html')).toBe(MKV);
  });

  it('keeps the original name when Content-Type is missing', () => {
    expect(f(MKV, null)).toBe(MKV);
    expect(f(MKV, '')).toBe(MKV);
    expect(f(MKV, undefined)).toBe(MKV);
  });

  it('appends the extension when the name has none', () => {
    expect(f('RO0017', 'video/mp4')).toBe('RO0017.mp4');
  });

  it('handles a missing filename', () => {
    expect(f(null, 'video/mp4')).toBe('clip.mp4');
    expect(f(undefined, null)).toBe('clip');
  });

  it('maps webm responses', () => {
    expect(f(MKV, 'video/webm')).toBe(
      'RO0017_20260709_214233_1_color.webm');
  });
});

describe('buildTrimmedShortclipUrl', () => {
  const f = ctx.buildTrimmedShortclipUrl;
  const BASE =
    '/api/archive/shortclip/RO0017/20260709/RO0017_20260709_214233_1_color.mkv';

  it('encodes the operator sub-selection as ss/t', () => {
    // Operator dragged handles to a 3.5 s window starting 8.2 s into a 20 s clip.
    const { url, ss, t, trimmed } =
      f(BASE, { start: 8.2, end: 11.7, duration: 20 });
    expect(url).toBe(BASE + '?ss=8.20&t=3.50');
    expect(ss).toBeCloseTo(8.2, 5);
    expect(t).toBeCloseTo(3.5, 5);
    expect(trimmed).toBe(true);
  });

  it('a leading-edge trim (start 0, end < duration) still cuts to t', () => {
    const { url, trimmed } = f(BASE, { start: 0, end: 5, duration: 20 });
    expect(url).toBe(BASE + '?ss=0.00&t=5.00');
    expect(trimmed).toBe(true);
  });

  it('full clip (start 0, end == duration) requests the whole clip, not trimmed', () => {
    const { url, ss, t, trimmed } = f(BASE, { start: 0, end: 20, duration: 20 });
    expect(url).toBe(BASE + '?ss=0.00&t=20.00');
    expect(ss).toBe(0);
    expect(t).toBeCloseTo(20, 5);
    expect(trimmed).toBe(false);
  });

  it('no trim state (null-ish fields) falls back to a full 0..duration cut', () => {
    const { url, trimmed } = f(BASE, { duration: 20 });
    expect(url).toBe(BASE + '?ss=0.00&t=20.00');
    expect(trimmed).toBe(false);
  });

  it('clamps end to the known duration', () => {
    // A stale end past the real duration must not overrun the clip.
    const { url, t } = f(BASE, { start: 2, end: 99, duration: 20 });
    expect(url).toBe(BASE + '?ss=2.00&t=18.00');
    expect(t).toBeCloseTo(18, 5);
  });

  it('enforces a minimum 0.5 s window for a degenerate selection', () => {
    const { t } = f(BASE, { start: 5, end: 5, duration: 20 });
    expect(t).toBeGreaterThanOrEqual(0.5);
  });

  it('with no known duration, a non-zero start still counts as trimmed', () => {
    const { url, trimmed } = f(BASE, { start: 4, end: 9 });
    expect(url).toBe(BASE + '?ss=4.00&t=5.00');
    expect(trimmed).toBe(true);
  });

  it('with no known duration and zero start, not treated as a sub-selection', () => {
    const { trimmed } = f(BASE, { start: 0, end: 6 });
    expect(trimmed).toBe(false);
  });
});

describe('formatUtcClock', () => {
  const f = ctx.formatUtcClock;

  it('formats a UTC instant as "YYYY-MM-DD HH:MM:SS UTC"', () => {
    // 2026-08-03T20:14:07Z
    expect(f(new Date(Date.UTC(2026, 7, 3, 20, 14, 7)))).toBe('2026-08-03 20:14:07 UTC');
  });

  it('zero-pads month, day, hours, minutes and seconds', () => {
    expect(f(new Date(Date.UTC(2026, 0, 5, 3, 4, 9)))).toBe('2026-01-05 03:04:09 UTC');
  });

  it('renders midnight as 00:00:00', () => {
    expect(f(new Date(Date.UTC(2026, 11, 31, 0, 0, 0)))).toBe('2026-12-31 00:00:00 UTC');
  });

  it('reads the UTC wall clock regardless of local timezone', () => {
    // A millisecond epoch that is a known UTC instant; getUTC* must ignore the
    // host TZ. 1_000_000_000_000 ms = 2001-09-09 01:46:40 UTC.
    expect(f(new Date(1_000_000_000_000))).toBe('2001-09-09 01:46:40 UTC');
  });

  it('returns an em dash for an invalid Date', () => {
    expect(f(new Date('not-a-date'))).toBe('—');
  });

  it('returns an em dash for non-Date input', () => {
    expect(f(null)).toBe('—');
    expect(f(undefined)).toBe('—');
    expect(f('2026-08-03')).toBe('—');
    expect(f(1_000_000_000_000)).toBe('—');
  });
});

describe('openAppPanel("about") branding + operator credit', () => {
  let fetchSpy;

  beforeEach(() => {
    // _loadNetworkStats() fetches /api/network-stats; a rejected fetch is
    // handled gracefully and doesn't affect the static About markup we assert.
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('no network in test'));
    document.body.innerHTML = `
      <div id="logo-dropdown"></div>
      <button id="logo-btn"></button>
      <div id="app-panel" class="app-panel">
        <span id="app-panel-title"></span>
        <div id="app-panel-body"></div>
      </div>`;
  });

  afterEach(() => {
    fetchSpy.mockRestore();
    document.body.innerHTML = '';
  });

  it('renders the operator credit as "Dr. Alexandru Tudorica"', () => {
    ctx.openAppPanel('about');
    const html = document.getElementById('app-panel-body').innerHTML;
    expect(html).toMatch(/Dr\. Alexandru Tudoric[aă]/);
    // The un-prefixed forms must not survive anywhere in the public credit.
    expect(html).not.toContain('>Alex Tudorica');
    expect(html).not.toContain('&bull; Alex Tudorica');
  });

  it('shows the GMN and Astromania partner logos in the About panel', () => {
    ctx.openAppPanel('about');
    const body = document.getElementById('app-panel-body');
    const gmn = body.querySelector('img[src="/static/img/gmn_sphere.png"]');
    const astro = body.querySelector('img[src="/static/img/astromania.png"]');
    expect(gmn).not.toBeNull();
    expect(astro).not.toBeNull();
    expect(astro.getAttribute('alt')).toBe('Astromania');
    expect(body.innerHTML).toContain('Partners');
  });

  it('sizes the two About partner logos to roughly equal area, each linking to its site', () => {
    ctx.openAppPanel('about');
    const body = document.getElementById('app-panel-body');
    // Only look at the partner-strip logos (the About prose has no other imgs).
    const gmn = body.querySelector('img[src="/static/img/gmn_sphere.png"]');
    const astro = body.querySelector('img[src="/static/img/astromania.png"]');
    // Equal-AREA sizing (not equal height): the near-square GMN sphere (160x159,
    // ~1:1) renders taller than the ~9.8:1 Astromania wordmark (2056x210), so
    // their rendered bounding boxes cover roughly the same area. astro_height
    // ~= gmn_height / sqrt(astro_aspect) => 44 / sqrt(9.79) ~= 14.
    expect(gmn.getAttribute('height')).toBe('44');
    expect(astro.getAttribute('height')).toBe('14');
    // The sphere is much taller than the wordmark (no longer equal height).
    expect(Number(gmn.getAttribute('height'))).toBeGreaterThan(
      Number(astro.getAttribute('height'))
    );
    // Sanity-check equal-area: rendered box areas within ~15% of each other.
    const gmnArea = 44 * 44 * (160 / 159);
    const astroArea = 14 * 14 * (2056 / 210);
    expect(Math.abs(gmnArea - astroArea) / gmnArea).toBeLessThan(0.15);
    // Each logo is wrapped in an anchor pointing at the partner's site,
    // opening in a new tab safely.
    const gmnLink = gmn.closest('a');
    const astroLink = astro.closest('a');
    expect(gmnLink).not.toBeNull();
    expect(astroLink).not.toBeNull();
    expect(gmnLink.getAttribute('href')).toBe('https://globalmeteornetwork.org');
    expect(astroLink.getAttribute('href')).toBe('https://astromania.org');
    for (const a of [gmnLink, astroLink]) {
      expect(a.getAttribute('target')).toBe('_blank');
      expect(a.getAttribute('rel')).toContain('noopener');
    }
  });

  it('lists the Florin + Alexandru contacts but drops the ROVIMEN network-mailbox line', () => {
    ctx.openAppPanel('about');
    const html = document.getElementById('app-panel-body').innerHTML;
    // The two operator contacts remain.
    expect(html).toContain('maintainer-2@example.org');
    expect(html).toContain('maintainer-1@example.org');
    // The generic ROVIMEN mailbox line is gone.
    expect(html).not.toContain('your-network@example.org');
    expect(html).not.toContain('ROVIMEN &mdash; <a href="mailto:your-network@example.org"');
  });
});

// ── Service state ────────────────────────────────────────────────────────────
// The station API reports "active (unmanaged)" when it found a service's
// process running without an active systemd unit behind it (legacy nohup /
// per-user installs). Every consumer must read that as up, or the dashboard
// paints a capturing camera dead.
describe('serviceIsUp', () => {
  const f = ctx.serviceIsUp;

  it('treats active as up', () => {
    expect(f('active')).toBe(true);
  });
  it('treats active (unmanaged) as up', () => {
    expect(f('active (unmanaged)')).toBe(true);
  });
  it('treats inactive as down', () => {
    expect(f('inactive')).toBe(false);
  });
  it('treats failed as down', () => {
    expect(f('failed')).toBe(false);
  });
  it('handles object payloads', () => {
    expect(f({ active: true })).toBe(true);
    expect(f({ state: 'active (unmanaged)' })).toBe(true);
    expect(f({ running: false, active: false })).toBe(false);
  });
  it('handles missing values', () => {
    expect(f(undefined)).toBe(false);
    expect(f(null)).toBe(false);
  });
});

describe('serviceStateClass', () => {
  const f = ctx.serviceStateClass;

  it('maps the four reported states', () => {
    expect(f('active')).toBe('active');
    expect(f('active (unmanaged)')).toBe('unmanaged');
    expect(f('inactive')).toBe('inactive');
    expect(f('failed')).toBe('failed');
  });
  it('falls back to unknown for anything else', () => {
    expect(f('reloading-ish')).toBe('unknown');
    expect(f(undefined)).toBe('unknown');
  });
  it('keeps object payloads renderable', () => {
    expect(f({ active: true })).toBe('active');
    expect(f({ active: false })).toBe('unknown');
  });
});

describe('segmentHealthBadge', () => {
  const f = ctx.segmentHealthBadge;

  it('describes a clip storm with its count and window', () => {
    const badge = f({ short: 1543, stalls: 0, window_min: 60, alarm: 'storm' });
    expect(badge.label).toBe('clip storm');
    expect(badge.cls).toBe('storm');
    expect(badge.title).toContain('last 60 min');
    expect(badge.title).toContain('1543 back-to-back segments');
  });

  it('describes stalls', () => {
    const badge = f({ short: 0, stalls: 123, window_min: 60, alarm: 'stalls' });
    expect(badge.label).toBe('capture stalls');
    expect(badge.cls).toBe('stalls');
    expect(badge.title).toContain('123 gaps');
  });

  it('colours a combined failure as a storm', () => {
    const badge = f({ short: 900, stalls: 30, window_min: 60, alarm: 'both' });
    expect(badge.label).toBe('clip storm + stalls');
    expect(badge.cls).toBe('storm');
  });

  it('shows nothing when the server did not raise an alarm', () => {
    // The threshold decision is server side; counters alone never badge.
    expect(f({ short: 1543, stalls: 99, alarm: null })).toBeNull();
    expect(f({ short: 0, stalls: 0, alarm: null })).toBeNull();
  });

  it('is null-safe on a camera the station does not report', () => {
    expect(f(undefined)).toBeNull();
    expect(f(null)).toBeNull();
    expect(f({})).toBeNull();
  });

  it('falls back to a generic label on an unknown alarm kind', () => {
    expect(f({ alarm: 'something-new' }).label).toBe('segment fault');
  });
});

describe('view state in the URL', () => {
  it('parses a full view', () => {
    expect(ctx.parseViewParams('?cam=RO000W&date=20260920&from=15:32&to=15:40')).toEqual(
      { cam: 'RO000W', date: '20260920', from: '15:32', to: '15:40' });
  });
  it('drops invalid values and keeps from/to only as a pair', () => {
    expect(ctx.parseViewParams('?cam=<x>&date=2026-09-20&from=15:32')).toEqual(
      { cam: null, date: null, from: null, to: null });
    expect(ctx.parseViewParams('?from=25:00&to=03:00').from).toBeNull();
  });
  it('passes extra keys through raw', () => {
    expect(ctx.parseViewParams('?tab=grid', ['tab']).tab).toBe('grid');
    expect(ctx.parseViewParams('', ['tab']).tab).toBeNull();
  });
  it('round-trips through viewQuery with readable colons', () => {
    const v = { cam: 'RO000W', date: '20260920', from: '15:32', to: '15:40' };
    const q = ctx.viewQuery(v);
    expect(q).toBe('?cam=RO000W&date=20260920&from=15:32&to=15:40');
    expect(ctx.parseViewParams(q)).toEqual(v);
  });
  it('omits unset keys and returns empty for an empty view', () => {
    expect(ctx.viewQuery({ cam: null, date: '20260920', from: null, to: null })).toBe('?date=20260920');
    expect(ctx.viewQuery({ cam: null, date: null })).toBe('');
    expect(ctx.viewQuery({ tab: 'grid', date: '20260920' }, ['tab', 'date'])).toBe('?tab=grid&date=20260920');
  });
});

describe('nearestNight', () => {
  const nights = ['20260925', '20260924', '20260920', '20260918'];
  it('returns the wanted night when present', () => {
    expect(ctx.nearestNight(nights, '20260920')).toBe('20260920');
  });
  it('falls back to the closest night, the older one on a tie', () => {
    expect(ctx.nearestNight(nights, '20260922')).toBe('20260920');
    expect(ctx.nearestNight(nights, '20260923')).toBe('20260924');
    expect(ctx.nearestNight(nights, '20260919')).toBe('20260918');
  });
  it('returns null without a wanted night or nights', () => {
    expect(ctx.nearestNight(nights, null)).toBeNull();
    expect(ctx.nearestNight([], '20260920')).toBeNull();
    expect(ctx.nearestNight(nights, 'bogus')).toBeNull();
  });
});

describe('createUrlSync', () => {
  let loc, replace, origLoc;
  beforeEach(() => {
    vi.useFakeTimers();
    origLoc = window.location;
    loc = { pathname: '/station/x/videodb', search: '' };
    Object.defineProperty(window, 'location', { value: loc, writable: true, configurable: true });
    replace = vi.spyOn(history, 'replaceState').mockImplementation((st, _t, url) => {
      const i = url.indexOf('?');
      loc.search = i >= 0 ? url.slice(i) : '';
      replace.lastState = st;
    });
  });
  afterEach(() => {
    vi.useRealTimers();
    replace.mockRestore();
    Object.defineProperty(window, 'location', { value: origLoc, writable: true, configurable: true });
  });
  it('coalesces bursts into one replaceState on the current path', () => {
    let q = '?from=15:00&to=15:10';
    const sync = ctx.createUrlSync(() => q);
    sync(); sync(); q = '?from=15:32&to=15:40'; sync();
    expect(replace).not.toHaveBeenCalled();
    vi.advanceTimersByTime(300);
    expect(replace).toHaveBeenCalledTimes(1);
    expect(replace.mock.calls[0][2]).toBe('/station/x/videodb?from=15:32&to=15:40');
  });
  it('flush writes immediately and skips a no-op write', () => {
    const sync = ctx.createUrlSync(() => '?cam=RO000W');
    sync.flush();
    expect(loc.search).toBe('?cam=RO000W');
    sync.flush();
    expect(replace).toHaveBeenCalledTimes(1);
  });
});

describe('clipStatus', () => {
  it('explains why a detection has no playable clip', () => {
    expect(ctx.clipStatus({ pending: true }).badge).toBe('NOT UPLOADED');
    expect(ctx.clipStatus({ pending: true }).label).toBe('not uploaded yet');
    expect(ctx.clipStatus({ pending: true, overdue: true }).label).toBe('upload overdue');
    expect(ctx.clipStatus({ pending: true, no_clip: true }).label).toBe('no video');
    expect(ctx.clipStatus({ pending: true, pruned: true }).label).toBe('removed');
    expect(ctx.clipStatus({ pending: true, pruned: true }).title).toMatch(/after 14 days/);
  });
});

describe('client events', () => {
  it('reports same-origin paths only, without the query', () => {
    expect(ctx.ceSamePath('/api/detections/20260921?x=1', 'https://d.example')).toBe('/api/detections/20260921');
    expect(ctx.ceSamePath('https://d.example/api/x', 'https://d.example')).toBe('/api/x');
    expect(ctx.ceSamePath('https://tiles.example/a.png', 'https://d.example')).toBeNull();
  });
  it('treats 5xx, timeouts and network errors as failures, not aborts or 4xx', () => {
    expect(ctx.ceFailureReason(504)).toBe('http');
    expect(ctx.ceFailureReason(404)).toBeNull();
    expect(ctx.ceFailureReason(200)).toBeNull();
    expect(ctx.ceFailureReason(0, { name: 'AbortError' })).toBeNull();
    expect(ctx.ceFailureReason(0, { name: 'TimeoutError' })).toBe('timeout');
    expect(ctx.ceFailureReason(0, new TypeError('Failed to fetch'))).toBe('network');
  });

  function fakeQueue(opts = {}) {
    const timers = [];
    const sent = [];
    let status = 204;
    const q = ctx.createClientEventQueue(ev => { sent.push(ev); return Promise.resolve(status); }, {
      flushMs: 10, batch: 2, maxPerPage: 5, maxPerKey: 2,
      setTimer: fn => { timers.push(fn); return timers.length; }, clearTimer: () => {},
      ...opts,
    });
    const tick = () => { const fn = timers.shift(); if (fn) fn(); };
    return { q, sent, tick, setStatus: s => { status = s; } };
  }

  it('batches, caps per page and per key', () => {
    const { q, sent, tick } = fakeQueue();
    expect(q.push({ n: 1 }, 'a')).toBe(true);
    expect(q.push({ n: 2 }, 'a')).toBe(true);
    expect(q.push({ n: 3 }, 'a')).toBe(false);          // per-key cap
    q.push({ n: 4 }); q.push({ n: 5 }); q.push({ n: 6 });
    expect(q.push({ n: 7 })).toBe(false);               // per-page cap (5)
    tick(); tick(); tick();
    expect(sent).toEqual([[{ n: 1 }, { n: 2 }], [{ n: 4 }, { n: 5 }], [{ n: 6 }]]);
  });

  it('stops for the page after a 401', async () => {
    const { q, sent, tick, setStatus } = fakeQueue();
    setStatus(401);
    q.push({ n: 1 });
    tick();
    await Promise.resolve(); await Promise.resolve();
    expect(q.push({ n: 2 })).toBe(false);
    expect(sent.length).toBe(1);
  });

  it('never throws when the sender does', () => {
    const q = ctx.createClientEventQueue(() => { throw new Error('boom'); },
      { setTimer: fn => { fn(); return 1; }, clearTimer: () => {} });
    expect(() => q.push({ n: 1 })).not.toThrow();
  });
});

describe('sameClip', () => {
  it('treats a clip and its details render as one clip', () => {
    expect(ctx.sameClip('X_color.mkv', 'X_color.details.mkv')).toBe(true);
    expect(ctx.sameClip('X_color.details.mp4', 'X_color.mp4')).toBe(true);
    expect(ctx.sameClip('X_color.mkv', 'Y_color.mkv')).toBe(false);
    expect(ctx.sameClip(null, 'X_color.mkv')).toBe(false);
  });
});

describe('fetchPaced', () => {
  const resp = (status, retryAfter) => ({
    status, ok: status < 400,
    headers: { get: h => (h === 'Retry-After' ? retryAfter : null) },
  });
  afterEach(() => { vi.unstubAllGlobals(); });

  it('waits out 429s with Retry-After, then returns the success', async () => {
    const seq = [resp(429, '12'), resp(429, null), resp(200)];
    vi.stubGlobal('fetch', vi.fn(async () => seq.shift()));
    const waits = [];
    const slept = [];
    const r = await ctx.fetchPaced('/x', s => waits.push(s), { sleep: async ms => { slept.push(ms); } });
    expect(r.status).toBe(200);
    expect(waits).toEqual([12, 15]);       // header, then the 15 s default
    expect(slept).toEqual([12000, 15000]);
  });

  it('gives up after `tries` and returns the last 429', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => resp(429, '1')));
    const r = await ctx.fetchPaced('/x', () => {}, { tries: 2, sleep: async () => {} });
    expect(r.status).toBe(429);
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it('waits out 202 "preparing" and 503 "remux busy" (3 s default)', async () => {
    const seq = [resp(202, '5'), resp(503, null), resp(200)];
    vi.stubGlobal('fetch', vi.fn(async () => seq.shift()));
    const waits = [];
    const r = await ctx.fetchPaced('/x', s => waits.push(s), { sleep: async () => {} });
    expect(r.status).toBe(200);
    expect(waits).toEqual([5, 3]);
  });
});

describe('saveDownload', () => {
  const resp = (status, headers = {}) => ({
    status, ok: status < 400,
    blob: async () => new Blob(['x']),
    headers: { get: h => headers[h.toLowerCase()] ?? null },
  });
  let clicked;
  beforeEach(() => {
    clicked = [];
    vi.useFakeTimers();
    URL.createObjectURL = () => 'blob:x';
    URL.revokeObjectURL = () => {};
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () { clicked.push(this.download); });
  });
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it('saves a clip that had to be prepared first, under the server name', async () => {
    const seq = [resp(202, { 'retry-after': '1' }),
      resp(200, { 'content-disposition': 'attachment; filename="A_color.mp4"', 'content-type': 'video/mp4' })];
    vi.stubGlobal('fetch', vi.fn(async () => seq.shift()));
    const waits = [];
    const done = ctx.saveDownload('/download/h/A/20260101/A_color.mkv', 'A_color.mkv', { onWait: s => waits.push(s) });
    await vi.advanceTimersByTimeAsync(1000);
    const r = await done;
    expect(r.status).toBe(200);
    expect(waits).toEqual([1]);
    expect(clicked).toEqual(['A_color.mp4']);
  });

  it('names the file after the type sent when there is no Content-Disposition', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => resp(200, { 'content-type': 'video/mp4' })));
    await ctx.saveDownload('/x', 'A_color.mkv');
    expect(clicked).toEqual(['A_color.mp4']);
  });

  it('hands the URL to the browser instead of reading the body into a blob', async () => {
    const blob = vi.fn();
    const hrefs = [];
    HTMLAnchorElement.prototype.click.mockImplementation(function () { hrefs.push(this.getAttribute('href')); });
    let signal;
    vi.stubGlobal('fetch', vi.fn(async (_u, init) => {
      signal = init?.signal;
      return { ...resp(200, { 'content-type': 'video/mp4' }), blob };
    }));
    await ctx.saveDownload('/api/stitch-multi/abc?name=x', 'x.mp4');
    expect(blob).not.toHaveBeenCalled();
    expect(signal?.aborted).toBe(true);
    expect(hrefs).toEqual(['/api/stitch-multi/abc?name=x']);
  });

  it('saves nothing for an error and says why', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => resp(404)));
    const r = await ctx.saveDownload('/x', 'A_color.mkv');
    expect(r.status).toBe(404);
    expect(clicked).toEqual([]);
    expect(ctx.downloadFailedText(404)).toBe('Clip not available');
    expect(ctx.downloadFailedText(202)).toMatch(/still being prepared/);
    expect(ctx.downloadFailedText(500)).toBe('Download failed (500)');
  });
});

describe('retryVideoIfPreparing', () => {
  const makeVideo = (src) => {
    const v = { _src: src, loads: 0 };
    v.getAttribute = (k) => (k === 'src' ? v._src : null);
    Object.defineProperty(v, 'src', { get: () => v._src, set: (x) => { v._src = x; } });
    v.load = () => { v.loads += 1; };
    return v;
  };
  const resp = (status, retryAfter) => ({
    status, ok: status >= 200 && status < 300 && status !== 202,
    headers: { get: (k) => (k === 'Retry-After' ? retryAfter : null) },
  });
  let origFetch;
  beforeEach(() => { vi.useFakeTimers(); origFetch = globalThis.fetch; });
  afterEach(() => { vi.useRealTimers(); globalThis.fetch = origFetch; });

  it('reloads after Retry-After when the clip is still being fetched', async () => {
    globalThis.fetch = vi.fn(async () => resp(202, '3'));
    const v = makeVideo('/api/cached-video/h/C/20260925/x.mkv?format=mp4');
    const onWaiting = vi.fn();
    expect(ctx.retryVideoIfPreparing(v, { onWaiting })).toBe(true);
    await vi.runAllTicks(); await Promise.resolve(); await Promise.resolve();
    expect(onWaiting).toHaveBeenCalledWith(3);
    expect(v.loads).toBe(0);
    vi.advanceTimersByTime(3000);
    expect(v.loads).toBe(1);
  });

  it('gives up on a real error and never retries blob sources', async () => {
    globalThis.fetch = vi.fn(async () => resp(404));
    const v = makeVideo('/api/cached-video/h/C/20260925/x.mkv');
    const onGiveUp = vi.fn();
    expect(ctx.retryVideoIfPreparing(v, { onGiveUp })).toBe(true);
    await Promise.resolve(); await Promise.resolve();
    expect(onGiveUp).toHaveBeenCalled();
    expect(ctx.retryVideoIfPreparing(makeVideo('blob:abc'))).toBe(false);
    expect(ctx.retryVideoIfPreparing(makeVideo(''))).toBe(false);
  });

  it('stops after PREPARING_MAX_TRIES and skips a reload once the src changed', async () => {
    globalThis.fetch = vi.fn(async () => resp(503, '1'));
    const v = makeVideo('/media/v1/clip/C/2026-09-06/x.mkv?format=mp4');
    v._prepTries = ctx.PREPARING_MAX_TRIES;
    expect(ctx.retryVideoIfPreparing(v)).toBe(false);
    v._prepTries = 0;
    ctx.retryVideoIfPreparing(v);
    await Promise.resolve(); await Promise.resolve();
    v._src = '';                          // modal closed
    vi.advanceTimersByTime(1000);
    expect(v.loads).toBe(0);
  });
});

describe('noteUntrimmed', () => {
  it('warns only when the server says the trim was not applied', () => {
    const hdr = v => ({ headers: { get: h => (h === 'X-Clip-Trimmed' ? v : null) } });
    const before = document.querySelectorAll('.toast').length;
    ctx.noteUntrimmed(hdr('1'));
    ctx.noteUntrimmed(hdr(null));
    expect(document.querySelectorAll('.toast').length).toBe(before);
    ctx.noteUntrimmed(hdr('0'));
    expect(document.querySelectorAll('.toast').length).toBe(before + 1);
    expect(() => ctx.noteUntrimmed(null)).not.toThrow();
  });
});

describe('networkRmsLabel', () => {
  const f = ctx.networkRmsLabel;
  it('gives the evening start time in daytime (UTC)', () => {
    // 20 Oct, noon UTC at the centre of Romania: capture has not started yet.
    expect(f(new Date(Date.UTC(2026, 9, 20, 12, 0, 0)))).toMatch(/^RMS starts 1[56]:\d{2} UTC$/);
  });
  it('says capture is running at night, with its end time', () => {
    expect(f(new Date(Date.UTC(2026, 9, 20, 22, 0, 0)))).toMatch(/^RMS recording · until 0[45]:\d{2} UTC$/);
  });
});

describe('utcSecondKey', () => {
  // #1397 gave detection time_utc milliseconds; the station page keyed its
  // detection lookup on the whole string and stopped finding any detection,
  // so clip cards lost their magnitude / shower / speed line.
  it('cuts a millisecond time_utc and a microsecond meteor_time to the same key', () => {
    expect(ctx.utcSecondKey('2026-10-21T19:00:22.155')).toBe('2026-10-21T19:00:22');
    expect(ctx.utcSecondKey('2026-10-21T19:00:22.155873')).toBe('2026-10-21T19:00:22');
    expect(ctx.utcSecondKey('2026-10-21T19:00:22')).toBe('2026-10-21T19:00:22');
  });
  it('returns null for a missing time', () => {
    expect(ctx.utcSecondKey(null)).toBe(null);
    expect(ctx.utcSecondKey('')).toBe(null);
  });
});

describe('keepClip', () => {
  // Distinct filenames per test: kept clips are remembered per page load.
  const clip = (n) => `RO0015_20260710_2230${String(n).padStart(2, '0')}_color.mp4`;
  const okFetch = () => vi.fn().mockResolvedValue({
    ok: true, json: () => Promise.resolve({ kept: true, locked: true, lock_type: 'manual' }),
  });

  beforeEach(() => {
    ctx.state.USER_ROLE = 'visitor';
    ctx._resetRoleCacheForTests();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('POSTs /api/keep once per clip for any account', async () => {
    const f = okFetch();
    vi.stubGlobal('fetch', f);
    const a = await ctx.keepClip('gmnro12', 'RO0015', '2026-07-10', clip(1));
    await ctx.keepClip('gmnro12', 'RO0015', '20260710', clip(1));
    expect(a).toEqual({ kept: true, locked: true, lock_type: 'manual' });
    expect(f).toHaveBeenCalledTimes(1);
    expect(f.mock.calls[0][0]).toBe(`/api/keep/gmnro12/RO0015/20260710/${clip(1)}`);
    expect(f.mock.calls[0][1]).toEqual({ method: 'POST' });
  });

  it('skips a guest session and clips without a full identity', async () => {
    const f = okFetch();
    vi.stubGlobal('fetch', f);
    expect(await ctx.keepClip('', 'RO0015', '20260710', clip(2))).toBeNull();
    expect(await ctx.keepClip('gmnro12', 'RO0015', '20260710', 'x_stack.webp')).toBeNull();
    expect(f).not.toHaveBeenCalled();
    ctx.state.USER_ROLE = 'guest';
    stubAuthStatus({ role: 'guest' });
    expect(await ctx.keepClip('gmnro12', 'RO0015', '20260710', clip(3))).toBeNull();
    expect(fetch).toHaveBeenCalledTimes(1);   // only the role lookup
  });

  it('retries on the next play after a failed POST, and never throws', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));
    expect(await ctx.keepClip('gmnro12', 'RO0015', '20260710', clip(4))).toBeNull();
    const f = okFetch();
    vi.stubGlobal('fetch', f);
    expect((await ctx.keepClip('gmnro12', 'RO0015', '20260710', clip(4))).kept).toBe(true);
    expect(f).toHaveBeenCalledTimes(1);
  });
});

// ── Shared helpers that replaced per-page copies ─────────────────────────────

describe('saveBlob', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('clicks a download link for the blob, then frees it', () => {
    vi.useFakeTimers();
    const blob = new Blob(['x']);
    const createObjectURL = vi.fn(() => 'blob:fake');
    const revokeObjectURL = vi.fn();
    vi.stubGlobal('URL', Object.assign(class extends URL {}, { createObjectURL, revokeObjectURL }));
    let clicked = null;
    let link = null;
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () {
      link = this;
      clicked = { href: this.getAttribute('href'), download: this.download, attached: this.isConnected };
    });
    try {
      ctx.saveBlob(blob, 'RO000V_20260730_214442_crop.mp4');
      expect(createObjectURL).toHaveBeenCalledWith(blob);
      expect(clicked).toEqual({ href: 'blob:fake', download: 'RO000V_20260730_214442_crop.mp4', attached: true });
      vi.advanceTimersByTime(1000);
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:fake');
      expect(link.isConnected).toBe(false);
    } finally {
      click.mockRestore();
    }
  });
});

describe('parseHHMM', () => {
  const f = ctx.parseHHMM;

  it('parses HH:MM, HHMM and H:MM to minutes of day', () => {
    expect(f('21:30')).toBe(21 * 60 + 30);
    expect(f('00:00')).toBe(0);
    expect(f('2130')).toBe(21 * 60 + 30);
    expect(f('3:05')).toBe(3 * 60 + 5);
    expect(f('  21:30  ')).toBe(21 * 60 + 30);
  });
  it('accepts 24:00 as the end of the day, nothing after it', () => {
    expect(f('24:00')).toBe(1440);
    expect(f('24:01')).toBeNull();
  });
  it('rejects empty, out-of-range and garbage input', () => {
    for (const bad of ['', '   ', null, undefined, '25:00', '21:60', 'abc', '9']) {
      expect(f(bad)).toBeNull();
    }
  });
});

describe('nightTitle', () => {
  const thisYear = new Date().getUTCFullYear();

  it('names a night of this year without the year', () => {
    expect(ctx.nightTitle(`${thisYear}1020`)).toBe('Night of 20 October');
  });
  it('adds the year for another year', () => {
    expect(ctx.nightTitle(`${thisYear - 1}0101`)).toBe(`Night of 1 January ${thisYear - 1}`);
  });
});

describe('rmsNightOf', () => {
  const f = ctx.rmsNightOf;

  it('files a time before noon UTC under the previous evening', () => {
    expect(f(new Date('2026-11-19T04:48:00Z'))).toBe('20261118');
    expect(f(new Date('2026-11-19T11:59:59Z'))).toBe('20261118');
  });
  it('files noon and after under the same day', () => {
    expect(f(new Date('2026-11-18T12:00:00Z'))).toBe('20261118');
    expect(f(new Date('2026-11-18T23:59:00Z'))).toBe('20261118');
  });
  it('crosses month and year boundaries', () => {
    expect(f(new Date('2026-12-01T02:00:00Z'))).toBe('20261130');
    expect(f(new Date('2027-01-01T02:00:00Z'))).toBe('20261231');
  });
  it('does not change the Date it is given', () => {
    const t = new Date('2026-11-19T04:48:00Z');
    f(t);
    expect(t.toISOString()).toBe('2026-11-19T04:48:00.000Z');
  });
});
