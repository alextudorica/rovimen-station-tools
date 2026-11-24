import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from 'vitest';

import { templateBody } from './helpers.js';

// dashboard-events.js looks up its page elements at import, so mount the real
// events.html body first, then import it.

const CLIP = { host_key: 'gmnro05', cam: 'RO0003', date: '20261120',
               filename: 'RO0003_20261120_201500_color.mkv', pre: 2, post: 5, label: '' };

let statusReply;
let statusCalls;

function stubFetch() {
  statusCalls = 0;
  vi.stubGlobal('fetch', vi.fn(async (url, opts) => {
    const r = await reply(url, opts);
    return { headers: { get: () => null }, ...r };
  }));
}

async function reply(url, opts) {
  const u = String(url);
  if (u === '/api/compilation' && opts?.method === 'POST') {
    return { ok: true, status: 201, json: async () => ({ id: 'comp-1' }) };
  }
  if (u.endsWith('/build')) return { ok: true, status: 202, json: async () => ({}) };
  if (u.endsWith('/status')) { statusCalls += 1; return statusReply(); }
  return { ok: true, status: 200, json: async () => ({}) };
}

const building = () => ({ ok: true, status: 200, json: async () => ({
  status: 'building', progress: { phase: 'fetch', done: 1, total: 2 } }) });
const progressText = () => document.getElementById('comp-progress').textContent;
const buildBtn = () => document.getElementById('comp-build-btn');

beforeAll(async () => {
  localStorage.setItem('rovimen_compilation_cart', JSON.stringify({ clips: [CLIP] }));
  document.body.innerHTML = templateBody('events.html');
  statusReply = building;
  stubFetch();
  await import('../../dashboard/static/dashboard-events.js');
  await new Promise(r => setTimeout(r, 0));   // _evInit loads the cart
});

describe('compilation build progress poll', () => {
  let hidden;
  beforeEach(() => {
    hidden = false;
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
    vi.useFakeTimers();
    stubFetch();
  });
  afterEach(() => {
    vi.useRealTimers();
    delete document.hidden;
  });

  async function startBuild() {
    await window.compCartBuild();
    expect(buildBtn().textContent).toBe('Building…');
  }

  it('stops when the build is done', async () => {
    statusReply = () => ({ ok: true, status: 200, json: async () => ({
      status: 'done', progress: { phase: 'done', done: 1, total: 1 } }) });
    await startBuild();
    await vi.advanceTimersByTimeAsync(1500);
    expect(progressText()).toContain('Done.');
    await vi.advanceTimersByTimeAsync(15000);
    expect(statusCalls).toBe(1);
    expect(buildBtn().textContent).toBe('Build now');
  });

  it('shows a failed build\'s error as text, never as markup', async () => {
    const err = 'ffmpeg said &lt;eof&gt; then <img src=x onerror="window.__xss=1"> & "q"';
    statusReply = () => ({ ok: true, status: 200, json: async () => ({
      status: 'error', error: err, progress: { phase: 'encode', done: 0, total: 1 } }) });
    await startBuild();
    await vi.advanceTimersByTimeAsync(1500);
    expect(progressText()).toContain(err);
    expect(document.querySelector('#comp-progress img')).toBeNull();
    expect(window.__xss).toBeUndefined();
  });

  it('gives up after three failed polls in a row', async () => {
    statusReply = () => Promise.reject(new TypeError('Failed to fetch'));
    await startBuild();
    await vi.advanceTimersByTimeAsync(1500 * 3);
    expect(statusCalls).toBe(3);
    await vi.advanceTimersByTimeAsync(60000);
    expect(statusCalls).toBe(3);
    expect(progressText()).toMatch(/status unknown.*reload/i);
    expect(buildBtn().textContent).toBe('Build now');
  });

  it('keeps polling through an error that is followed by an answer', async () => {
    let n = 0;
    statusReply = () => (++n % 3 === 0 ? building()
      : { ok: false, status: 502, json: async () => ({}) });
    await startBuild();
    await vi.advanceTimersByTimeAsync(1500 * 12);
    expect(statusCalls).toBe(12);
    expect(progressText()).not.toMatch(/unknown/i);
  });

  it('gives up after 30 minutes of "building" (a restart can leave it there)', async () => {
    statusReply = building;
    await startBuild();
    await vi.advanceTimersByTimeAsync(30 * 60 * 1000 + 3000);
    const calls = statusCalls;
    expect(calls).toBeGreaterThan(1000);
    expect(calls).toBeLessThanOrEqual(1201);
    await vi.advanceTimersByTimeAsync(60000);
    expect(statusCalls).toBe(calls);
    expect(progressText()).toMatch(/status unknown.*reload/i);
  });

  it('does not poll while the tab is hidden', async () => {
    statusReply = building;
    await startBuild();
    hidden = true;
    await vi.advanceTimersByTimeAsync(15000);
    expect(statusCalls).toBe(0);
    hidden = false;
    await vi.advanceTimersByTimeAsync(1500);
    expect(statusCalls).toBe(1);
  });
});
