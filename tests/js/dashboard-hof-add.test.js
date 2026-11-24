import { describe, it, expect, vi, beforeEach } from 'vitest';

// ── Hall of Fame add (dashboard-hof-add.js) ──────────────────────────────────
//
// Only archived clips can be featured: the server answers 404 until the clip is
// in the archive. A station clip that is not there yet gets locked (which makes
// the station re-encode and upload it) and is added once the upload lands.

const CLIP = { camera: 'TST001', date: '20260601', filename: 'TST001_20260601_220000_color.mkv' };

function stubFetch(postStatuses) {
  const posts = [];
  vi.stubGlobal('fetch', vi.fn(async (url, opts = {}) => {
    const method = opts.method || 'GET';
    if (url === '/api/auth/status') return { ok: true, status: 200, json: async () => ({ admin: true }) };
    if (url === '/api/hall_of_fame' && method === 'GET') return { ok: true, status: 200, json: async () => ({ entries: [] }) };
    if (url === '/api/hall_of_fame' && method === 'POST') {
      const status = postStatuses[Math.min(posts.length, postStatuses.length - 1)];
      posts.push(status);
      return { ok: status < 400, status, json: async () => (status < 400 ? {} : { error: 'clip not found in the public archive' }) };
    }
    return { ok: false, status: 500, json: async () => ({}) };
  }));
  return posts;
}

async function importFresh() {
  vi.resetModules();
  const mod = await import('../../dashboard/static/dashboard-hof-add.js');
  mod.hofArchiveWait.everyMs = 0;
  mod.hofArchiveWait.tries = 3;
  return mod;
}

beforeEach(() => {
  vi.unstubAllGlobals();
  vi.stubGlobal('alert', vi.fn());
});

describe('wireHofButton', () => {
  it('adds an archived clip directly, without locking it', async () => {
    const posts = stubFetch([201]);
    const { wireHofButton } = await importFresh();
    const btn = document.createElement('button');
    const lockForUpload = vi.fn(async () => {});
    await wireHofButton(btn, { ...CLIP, lockForUpload });
    await btn.onclick();
    expect(lockForUpload).not.toHaveBeenCalled();
    expect(posts).toEqual([201]);
    expect(btn.textContent).toContain('In Hall of Fame');
  });

  it('locks a clip that is not archived yet, then adds it once uploaded', async () => {
    const posts = stubFetch([404, 404, 201]);
    const { wireHofButton } = await importFresh();
    const btn = document.createElement('button');
    const lockForUpload = vi.fn(async () => {});
    await wireHofButton(btn, { ...CLIP, lockForUpload });
    await btn.onclick();
    expect(lockForUpload).toHaveBeenCalledTimes(1);
    expect(posts).toEqual([404, 404, 201]);
    expect(btn.textContent).toContain('In Hall of Fame');
    expect(btn.disabled).toBe(false);
  });

  it('gives up after the wait and says so', async () => {
    stubFetch([404]);
    const { wireHofButton } = await importFresh();
    const btn = document.createElement('button');
    await wireHofButton(btn, { ...CLIP, lockForUpload: async () => {} });
    await btn.onclick();
    expect(alert).toHaveBeenCalledWith(expect.stringContaining('still not in the archive'));
    expect(btn.textContent).toContain('Add to Hall of Fame');
  });

  it('without lockForUpload (an archive-only surface) reports the 404 as before', async () => {
    stubFetch([404]);
    const { wireHofButton } = await importFresh();
    const btn = document.createElement('button');
    await wireHofButton(btn, CLIP);
    await btn.onclick();
    expect(alert).toHaveBeenCalledWith(expect.stringContaining('not found in the public archive'));
  });
});
