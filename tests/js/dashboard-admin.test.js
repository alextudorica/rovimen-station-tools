import { describe, it, expect, beforeAll } from 'vitest';

// dashboard-admin.js has module-load side effects: loadConfig() (fetch of an
// admin endpoint — settles as a suppressed unhandled rejection, see setup.js)
// and a delegated click handler bound to #users-area. Provide the mount, then
// dynamic-import so those side effects don't throw in jsdom.
let ctx;
beforeAll(async () => {
  const usersArea = document.createElement('div');
  usersArea.id = 'users-area';
  document.body.appendChild(usersArea);
  ctx = await import('../../dashboard/static/dashboard-admin.js');
});

describe('_trafficDailyLayout', () => {
  it('computes stacked heights as percentages of the tallest day', () => {
    const { max, cols } = ctx._trafficDailyLayout([
      { date: '2026-07-27', auth: 5, anon: 10, bot: 5 },
      { date: '2026-07-28', auth: 1, anon: 2, bot: 1 },
    ]);
    expect(max).toBe(20);
    expect(cols[0].hAuth).toBe(25);
    expect(cols[0].hAnon).toBe(50);
    expect(cols[0].hBot).toBe(25);
    expect(cols[1].total).toBe(4);
    expect(cols[1].hAnon).toBe(10);
  });

  it('tolerates missing keys on old-shape rows', () => {
    const { cols } = ctx._trafficDailyLayout([{ date: '2026-07-28' }, {}]);
    expect(cols[0].total).toBe(0);
    expect(cols[0].hAuth).toBe(0);
    expect(cols[1].date).toBe('');
    expect(cols[1].label).toBe('');
  });

  it('guards against a zero-visit window (no divide-by-zero)', () => {
    const { max, cols } = ctx._trafficDailyLayout([
      { date: '2026-07-28', auth: 0, anon: 0, bot: 0 },
    ]);
    expect(max).toBe(1);
    expect(cols[0].hAnon).toBe(0);
  });

  it('thins labels on wide windows like the usage chart', () => {
    const wide = Array.from({ length: 60 }, (_, i) => ({
      date: `2026-06-${String(i + 1).padStart(2, '0')}`, auth: 1,
    }));
    const { step, cols } = ctx._trafficDailyLayout(wide);
    expect(step).toBe(7);
    expect(cols[0].label).toBe('06-01');
    expect(cols[1].label).toBe('');
    expect(cols[7].label).toBe('06-08');
  });

  it('handles non-array input', () => {
    expect(ctx._trafficDailyLayout(undefined).cols).toEqual([]);
    expect(ctx._trafficDailyLayout(null).max).toBe(1);
  });
});

describe('_countryLabel', () => {
  it('expands a known ISO code', () => {
    expect(ctx._countryLabel('RO')).toBe('RO — Romania');
  });

  it('maps ?? (no Cloudflare header) to the direct-traffic label', () => {
    expect(ctx._countryLabel('??')).toBe('Unknown (direct)');
    expect(ctx._countryLabel('')).toBe('Unknown (direct)');
    expect(ctx._countryLabel(null)).toBe('Unknown (direct)');
  });

  it('falls back to the raw code for non-ISO values like T1 (Tor)', () => {
    expect(ctx._countryLabel('T1')).toBe('T1');
  });
});
