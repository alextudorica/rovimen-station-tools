import { describe, it, expect, beforeAll, beforeEach, vi } from 'vitest';

import { state } from '../../dashboard/static/dashboard-common.js';
import * as ctx from '../../dashboard/static/dashboard-sysadmin.js';

// The whole System Admin tab lives in this module: rendering it needs only
// the station page's helpers that dashboard-station.js puts on window.
describe('System Admin tab', () => {
  const H = 'stn_a';
  const fetched = [];
  const pane = id => document.getElementById(`sa-pane-${id}-${H}`);

  beforeAll(() => {
    HTMLCanvasElement.prototype.getContext = () => ({ clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {} });
    globalThis.fetch = vi.fn(async url => { fetched.push(String(url)); return new Response('{}', { status: 404 }); });
    Object.assign(window, {
      mkCameraWindows: host => `<div class="cams-${host}">cameras</div>`,
      mkServices: () => '<ul class="svc"></ul>',
      mkStorage: () => '<div class="sto"></div>',
      _ownsStation: () => true,
      fetchOnce: (_key, url) => fetch(url),
      _isAbort: e => !!e && e.name === 'AbortError',
      hwData: {}, encStatsData: {}, vitalsHistory: {}, coreData: {}, cronsData: {}, storagewatchData: {},
      twilightSlider: { sliderValToHHMM: () => '00:00', drawTwilightOnWrap() {} },
    });
    state.statusData[H] = { online: true, services: { 'color-capture': 'active' } };
  });

  beforeEach(() => {
    document.body.innerHTML = '<div id="pane-settings"></div>';
    window.settingsData = { [H]: { compression_level: 1, stations: { AA0001: { rotate: true } } } };
    state.IS_ADMIN = true;
    fetched.length = 0;
  });

  it('renders Health, Configuration, Logs and Live view, Health shown first', () => {
    window.renderSysAdmin(H);
    expect([...document.querySelectorAll('#pane-settings [role=tab]')].map(b => b.textContent))
      .toEqual(['Health', 'Configuration', 'Logs', 'Live view']);
    expect(document.getElementById(`sa-tab-health-${H}`).getAttribute('aria-selected')).toBe('true');
    expect(pane('health').hidden).toBe(false);
    expect(pane('processes').hidden).toBe(true);
    expect(pane('processes').textContent).toContain('Show config.json');
    expect(pane('livestream').querySelector(`.cams-${H}`)).not.toBeNull();
  });

  it('switches sub-tab and refreshes the night only while Health is shown', () => {
    window.renderSysAdmin(H);
    window.switchSysAdminTab(H, 'processes');
    expect(pane('processes').hidden).toBe(false);
    expect(pane('health').hidden).toBe(true);
    fetched.length = 0;
    window.refreshNightIfShown(H);
    expect(fetched.filter(u => u.startsWith('/api/station_night/'))).toEqual([]);
    window.switchSysAdminTab(H, 'health');
    fetched.length = 0;
    window.refreshNightIfShown(H);
    expect(fetched.filter(u => u.startsWith('/api/station_night/'))).toHaveLength(1);
  });

  it('rebuilds only when data baked into the tab changed', () => {
    window.renderSysAdmin(H);
    const root = document.getElementById(`sysadmin-root-${H}`);
    window.renderSysAdminIfChanged(H);
    expect(document.getElementById(`sysadmin-root-${H}`)).toBe(root);
    window.settingsData[H] = { ...window.settingsData[H], compression_level: 3 };
    window.renderSysAdminIfChanged(H);
    expect(document.getElementById(`sysadmin-root-${H}`)).not.toBe(root);
    expect(pane('processes').querySelector('[aria-current="true"]').textContent).toContain('QP 21');
  });

  it('shows a station owner who is not an admin the live view alone', () => {
    state.IS_ADMIN = false;
    window.renderSysAdmin(H);
    expect(document.getElementById(`sysadmin-host-${H}`).textContent).toBe('cameras');
    expect(document.querySelector('#pane-settings [role=tab]')).toBeNull();
  });

  it('puts every function its inline handlers call on window', () => {
    for (const name of ['switchSysAdminTab', 'refreshSysAdmin', 'nightNavDate', 'logoPicked', 'saveOverlayLogo',
      'previewOverlay', 'saveDataVisibility', '_lastNight', 'renderSysAdmin', 'renderSysAdminIfChanged', 'refreshNightIfShown']) {
      expect(typeof window[name], name).toBe('function');
    }
  });
});

describe('_stationEventsHtml', () => {
  const f = ctx._stationEventsHtml;

  it('says so when there are no events', () => {
    expect(f([])).toContain('No events recorded yet');
    expect(f(undefined)).toContain('No events recorded yet');
  });

  it('renders time UTC, label and the detail text', () => {
    const html = f([{ ts: '2026-11-13T02:14:00Z', kind: 'boot',
      detail: { text: 'booted; the previous boot ended abruptly', down_s: 60 } }]);
    expect(html).toContain('2026-11-13 02:14:00');
    expect(html).toContain('<td>Boot</td>');
    expect(html).toContain('booted; the previous boot ended abruptly');
  });

  it('shows whether the station or the VPS recorded the event', () => {
    const html = f([
      { ts: '2026-11-13T02:00:00Z', kind: 'offline', source: 'vps', detail: { text: 'no heartbeat' } },
      { ts: '2026-11-13T01:00:00Z', kind: 'uplink', source: 'station', detail: { text: 'uplink ok -> isp' } },
    ]);
    expect(html).toContain('<th scope="col">Source</th>');
    expect(html).toContain('<td class="sa-note">VPS</td>');
    expect(html).toContain('<td class="sa-note">Station</td>');
  });

  it('marks a clamped station time and names the time it claimed', () => {
    const html = f([{ ts: '2026-11-21T11:50:00Z', received_at: '2026-11-21T11:50:00Z', kind: 'uplink',
      source: 'station', ts_clamped: true, sent_ts: '2027-11-21T00:00:00Z', detail: { text: 'x' } }]);
    expect(html).toContain('2026-11-21 11:50:00 *');
    expect(html).toContain('Station sent 2027-11-21T00:00:00Z');
  });

  it('escapes station-sent text', () => {
    const html = f([{ ts: '2026-11-13T02:14:00Z', kind: 'station_log', detail: '<img src=x onerror=1>' }]);
    expect(html).not.toContain('<img');
    expect(html).toContain('&lt;img');
  });

  it('falls back to the raw kind and JSON detail', () => {
    const html = f([{ ts: '2026-11-13T02:14:00Z', kind: 'mystery', detail: { a: 1 } }]);
    expect(html).toContain('mystery');
    expect(html).toContain('{&quot;a&quot;:1}');
  });
});

describe('_parseCronSchedule', () => {
  const f = ctx._parseCronSchedule;

  it('@reboot', () => {
    expect(f('@reboot')).toEqual({ label: 'At reboot', detail: '' });
  });
  it('@hourly', () => {
    expect(f('@hourly')).toEqual({ label: 'Every hour', detail: 'at :00' });
  });
  it('@daily', () => {
    expect(f('@daily')).toEqual({ label: 'Daily', detail: 'at midnight UTC' });
  });
  it('@midnight (alias for daily)', () => {
    expect(f('@midnight')).toEqual({ label: 'Daily', detail: 'at midnight UTC' });
  });
  it('@weekly', () => {
    expect(f('@weekly')).toEqual({ label: 'Weekly', detail: 'Sunday midnight' });
  });
  it('@monthly', () => {
    expect(f('@monthly')).toEqual({ label: 'Monthly', detail: '1st at midnight' });
  });
  it('every 5 minutes: */5 * * * *', () => {
    expect(f('*/5 * * * *')).toEqual({ label: 'Every 5 min', detail: '' });
  });
  it('every 10 minutes during hour range: */10 4-10 * * *', () => {
    const r = f('*/10 4-10 * * *');
    expect(r.label).toBe('Every 10 min');
    expect(r.detail).toBe('04:00–10:59 UTC');
  });
  it('every N minutes restricted to specific hour: */15 8 * * *', () => {
    const r = f('*/15 8 * * *');
    expect(r.label).toBe('Every 15 min');
    expect(r.detail).toBe('hour 8 UTC');
  });
  it('stepped minute range: 5-55/10 * * * *', () => {
    expect(f('5-55/10 * * * *')).toEqual({ label: 'Every 10 min', detail: '' });
  });
  it('every 2 hours at :30: 30 */2 * * *', () => {
    expect(f('30 */2 * * *')).toEqual({ label: 'Every 2h', detail: 'at :30' });
  });
  it('every minute: * * * * *', () => {
    expect(f('* * * * *')).toEqual({ label: 'Every minute', detail: '' });
  });
  it('hourly at :15: 15 * * * *', () => {
    expect(f('15 * * * *')).toEqual({ label: 'Hourly', detail: 'at :15' });
  });
  it('hourly at :05 (single digit): 5 * * * *', () => {
    expect(f('5 * * * *')).toEqual({ label: 'Hourly', detail: 'at :05' });
  });
  it('weekly on Wednesday: 0 3 * * 3', () => {
    expect(f('0 3 * * 3')).toEqual({ label: 'Weekly (Wed)', detail: 'at 03:00 UTC' });
  });
  it('weekly on Sunday: 30 2 * * 0', () => {
    expect(f('30 2 * * 0')).toEqual({ label: 'Weekly (Sun)', detail: 'at 02:30 UTC' });
  });
  it('daily at 14:30: 30 14 * * *', () => {
    expect(f('30 14 * * *')).toEqual({ label: 'Daily', detail: 'at 14:30 UTC' });
  });
  it('daily at 03:00: 0 3 * * *', () => {
    expect(f('0 3 * * *')).toEqual({ label: 'Daily', detail: 'at 03:00 UTC' });
  });
  it('fallback for complex schedule: 0 3 1,15 * *', () => {
    const r = f('0 3 1,15 * *');
    expect(r.label).toBe('0 3 1,15 * *');
    expect(r.detail).toBe('');
  });
  it('handles extra whitespace', () => {
    expect(f('  @reboot  ')).toEqual({ label: 'At reboot', detail: '' });
  });
});

describe('_logLineLevel', () => {
  const f = ctx._logLineLevel;

  it('classifies ERROR lines', () => {
    expect(f('2026-06-02 ERROR something broke')).toBe('error');
  });
  it('classifies CRITICAL as error', () => {
    expect(f('CRITICAL: disk full')).toBe('error');
  });
  it('classifies WARNING lines', () => {
    expect(f('2026-06-02 WARNING disk 90%')).toBe('warning');
  });
  it('classifies WARN lines', () => {
    expect(f('[WARN] low memory')).toBe('warning');
  });
  it('classifies DEBUG lines', () => {
    expect(f('DEBUG: entering function')).toBe('debug');
  });
  it('defaults to info for normal lines', () => {
    expect(f('Starting server on port 17777')).toBe('info');
  });
  it('case insensitive', () => {
    expect(f('error: something')).toBe('error');
    expect(f('warning: something')).toBe('warning');
    expect(f('debug: something')).toBe('debug');
  });
  it('empty string is info', () => {
    expect(f('')).toBe('info');
  });
});

describe('_splitLogLine', () => {
  const f = ctx._splitLogLine;

  it('splits Python logging lines', () => {
    expect(f('2026-10-22 05:19:41,123 INFO capture started'))
      .toEqual({ time: '05:19:41', msg: 'capture started' });
  });
  it('splits ISO timestamps with a zone', () => {
    expect(f('2026-10-22T02:14:09Z ffmpeg exited')).toEqual({ time: '02:14:09', msg: 'ffmpeg exited' });
  });
  it('drops the host and unit of journald lines', () => {
    expect(f('Oct 22 02:14:09 gmnro003 color-capture[811]: RO000K reconnect failed'))
      .toEqual({ time: '02:14:09', msg: 'RO000K reconnect failed' });
    expect(f('Oct 22 02:14:31 h color-capture[811]: ERROR: TST002 down').msg).toBe('TST002 down');
  });
  it('keeps anything else whole', () => {
    expect(f('Traceback (most recent call last):')).toEqual({ time: '', msg: 'Traceback (most recent call last):' });
  });
});

describe('nightLaneStatus', () => {
  const f = ctx.nightLaneStatus;
  const c = o => ({ total: 100, stacked: 100, reencoded: 0, locked: 3, uploaded: 3,
                    rms_complete: true, morning_done: true, ...o });

  it('says a camera without a pushed state.json is not uploaded yet, or has no data on an old night', () => {
    expect(f(null).text).toBe('Not uploaded yet');
    expect(f(null, true).text).toBe('No station data for this night');
  });
  it('flags a night with no colour video', () => {
    expect(f(c({ total: 0 }))).toEqual({ text: 'No colour video', tone: 'bad' });
  });
  it('trusts morning_done, whatever the counts say', () => {
    // full-night re-encode off: unlocked chunks stay unencoded for good
    expect(f(c({ reencoded: 3 })).text).toBe('Done');
  });
  it('waits for RMS before anything else', () => {
    expect(f(c({ rms_complete: false, morning_done: false })).text).toBe('Waiting for RMS');
  });
  it('says processing while dawn runs, even with every clip uploaded', () => {
    expect(f(c({ morning_done: false }))).toEqual({ text: 'Processing', tone: 'busy' });
  });
  it('without morning_done, calls only old nights done', () => {
    expect(f(c({ morning_done: null }), true).text).toBe('Done');
    expect(f(c({ morning_done: null })).text).toBe('Processing');
  });
});

describe('nightGaps', () => {
  const f = ctx.nightGaps;

  it('returns the holes of two minutes or more between spans', () => {
    expect(f([[240, 300], [301, 400], [410, 500]])).toEqual([[400, 410]]);
  });
  it('has none for a single span or none', () => {
    expect(f([[240, 900]])).toEqual([]);
    expect(f([])).toEqual([]);
  });
});

describe('nightStageBars', () => {
  const f = ctx.nightStageBars;
  const night = (stages, o = {}) => ({ spans: [[245, 1040]], morning_done: true, stages, ...o });

  it('draws nothing for a night from before stage_times', () => {
    expect(f(night({}))).toEqual([]);
    expect(f(null)).toEqual([]);
  });
  it('runs morning processing from dawn start to finish, uploads first to last', () => {
    // dawn starts (rms_complete) before colour capture ends: no RMS bar any more
    expect(f(night({ rms_complete: 960, morning_done: 1150, first_upload: 991, last_upload: 1010 }))).toEqual([
      { kind: 'proc', from: 960, to: 1150, running: false },
      { kind: 'up', from: 991, to: 1010 },
    ]);
  });
  it('draws a running dawn up to now, and none without a now', () => {
    const c = night({ rms_complete: 960 }, { morning_done: false });
    expect(f(c, 1120)).toEqual([{ kind: 'proc', from: 960, to: 1120, running: true }]);
    expect(f(c)).toEqual([]);
  });
});

describe('nightStageCounts', () => {
  const f = ctx.nightStageCounts;
  const c = o => ({ total: 2199, stacked: 2199, reencoded: 1719, locked: 35, uploaded: 35,
                    rms_complete: true, morning_done: false, timelapse_done: true, ...o });

  it('lists each step with its count', () => {
    expect(f(c()).map(x => `${x.label} ${x.value}`))
      .toEqual(['Stacked 2,199/2,199', 'Encoded 1,719/2,199', 'Timelapse ✓', 'Clips uploaded 35/35']);
  });
  it('dims a count a finished night legitimately stopped short of, with the reason', () => {
    const enc = f(c({ morning_done: true }))[1];
    expect(enc.dim).toBe(true);
    expect(enc.title).toMatch(/Full-night re-encode/);
    expect(f(c())[1].dim).toBe(false);  // still running: not dimmed
  });
  it('leaves out the timelapse when the index predates it, and everything without video', () => {
    expect(f(c({ timelapse_done: null })).map(x => x.label)).not.toContain('Timelapse');
    expect(f(c({ total: 0 }))).toEqual([]);
    expect(f(null)).toEqual([]);
  });
});

describe('_updateProblem', () => {
  const f = ctx._updateProblem;

  it('is null without an update record (older stations)', () => {
    expect(f({ local: '549.2026.11.19', bundle: { build: 549 } })).toBe(null);
    expect(f({})).toBe(null);
  });
  it('names the services that did not come back', () => {
    const d = { local: '550.x', bundle: { update: {
      status: 'unhealthy', version: '550.x', failed_services: ['color-capture', 'rovimen-pusher'] } } };
    expect(f(d)).toBe('update 550.x: not running after restart: color-capture, rovimen-pusher');
  });
  it('reports a failed apply', () => {
    const d = { local: '549.x', bundle: { update: { status: 'failed', version: '550.x', detail: 'apply rsync failed' } } };
    expect(f(d)).toBe('update 550.x failed: apply rsync failed');
  });
  it('reports an update that started but did not complete', () => {
    const d = { local: '549.x', bundle: { update: { status: 'started', version: '550.x' } } };
    expect(f(d)).toContain('not completed');
  });
  it('is null for a completed healthy update', () => {
    expect(f({ local: '550.x', bundle: { update: { status: 'ok', version: '550.x' } } })).toBe(null);
    expect(f({ local: '550.x', bundle: { update: { status: 'applied', version: '550.x' } } })).toBe(null);
  });
});
