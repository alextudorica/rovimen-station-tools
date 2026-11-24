import { describe, it, expect } from 'vitest';

// The module builds its video modal on load, inside this container.
document.body.innerHTML = '<div id="arc-modal-container"></div>';
const ctx = await import('../../dashboard/static/dashboard-archive.js');

describe('archiveCalendar', () => {
  const f = ctx.archiveCalendar;
  const n = (date, clips, timelapse = true) => ({ date, clips, timelapse });

  it('lays nights out in Monday-first weeks from the first night to the last', () => {
    // 2026-10-14 is a Wednesday, 2026-10-21 the next Wednesday
    const cal = f([n('20261021', 75), n('20261014', 10), n('20261018', 0, false)]);
    expect(cal.weeks).toHaveLength(2);
    expect(cal.weeks[0][0]).toBeNull();                       // Monday 12th, before the first night
    expect(cal.weeks[0][2]).toMatchObject({ date: '20261014', clips: 10 });
    expect(cal.weeks[0][3]).toMatchObject({ date: '20261015', clips: 0, timelapse: false });  // gap
    expect(cal.weeks[1][2]).toMatchObject({ date: '20261021', clips: 75, level: 4 });
    expect(cal.weeks[1][3]).toBeNull();                       // after the last night
  });
  it('scales levels to the busiest night and names it', () => {
    const cal = f([n('20261001', 100), n('20261002', 30), n('20261003', 1)]);
    expect(cal.weeks.flat().filter(Boolean).map(d => d.level)).toEqual([4, 2, 1]);
    expect(cal.busiest).toEqual({ date: '20261001', clips: 100 });
  });
  it('labels each month where it starts, and the first one when there is room', () => {
    const cal = f([n('20260910', 5), n('20261020', 5)]);
    expect(cal.months.map(m => new Date(m.ms).getUTCMonth())).toEqual([8, 9]);
    // a month starting one column in leaves no room for the first label
    expect(f([n('20260925', 5), n('20261005', 5)]).months.map(m => new Date(m.ms).getUTCMonth())).toEqual([9]);
  });
  it('is empty without nights', () => {
    expect(f([])).toEqual({ weeks: [], months: [], max: 0, busiest: null });
  });
});
