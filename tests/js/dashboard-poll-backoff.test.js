/**
 * Backoff schedule for the live-thumbnail pollers.
 *
 * A browser left open overnight polled /api/latest_ff_maxpixel every 15 s per
 * camera regardless of whether the station answered, turning a few offline
 * cameras into 3,751 x 503 between 00:00 and 05:00. createPollBackoff() is the
 * schedule that stops that; these tests pin the ramp, the ceiling and the
 * reset-on-success so a future edit can't quietly restore the hammering.
 */
import { describe, it, expect } from 'vitest';

import { createPollBackoff, POLL_BACKOFF_STEPS_MS } from '../../dashboard/static/dashboard-common.js';

const S = 1000;
const MIN = 60 * S;

/** A backoff with a hand-cranked clock, so no timers are involved. */
function fakeClock(start = 0) {
  let t = start;
  return { now: () => t, advance: (ms) => { t += ms; } };
}

describe('POLL_BACKOFF_STEPS_MS', () => {
  it('ramps 30 s -> 1 min -> 5 min -> 15 min', () => {
    expect(POLL_BACKOFF_STEPS_MS).toEqual([30 * S, 1 * MIN, 5 * MIN, 15 * MIN]);
  });
});

describe('createPollBackoff schedule', () => {
  it('returns each step in order for consecutive failures', () => {
    const clock = fakeClock();
    const b = createPollBackoff(POLL_BACKOFF_STEPS_MS, clock.now);
    expect(b.fail('a')).toBe(30 * S);
    expect(b.fail('a')).toBe(1 * MIN);
    expect(b.fail('a')).toBe(5 * MIN);
    expect(b.fail('a')).toBe(15 * MIN);
  });

  it('caps at the last step instead of growing without bound', () => {
    const clock = fakeClock();
    const b = createPollBackoff(POLL_BACKOFF_STEPS_MS, clock.now);
    for (let i = 0; i < 4; i++) b.fail('a');
    for (let i = 0; i < 20; i++) expect(b.fail('a')).toBe(15 * MIN);
    expect(b.failures('a')).toBe(24);
  });

  it('delayFor is 1-based and clamps both ends', () => {
    const b = createPollBackoff();
    expect(b.delayFor(1)).toBe(30 * S);
    expect(b.delayFor(0)).toBe(30 * S);
    expect(b.delayFor(-3)).toBe(30 * S);
    expect(b.delayFor(4)).toBe(15 * MIN);
    expect(b.delayFor(999)).toBe(15 * MIN);
  });
});

describe('createPollBackoff blocking window', () => {
  it('does not block a key that has never failed', () => {
    const b = createPollBackoff(POLL_BACKOFF_STEPS_MS, fakeClock().now);
    expect(b.blocked('a')).toBe(false);
  });

  it('blocks for exactly the step and unblocks after it elapses', () => {
    const clock = fakeClock();
    const b = createPollBackoff(POLL_BACKOFF_STEPS_MS, clock.now);
    b.fail('a');
    expect(b.blocked('a')).toBe(true);
    clock.advance(30 * S - 1);
    expect(b.blocked('a')).toBe(true);
    clock.advance(1);
    expect(b.blocked('a')).toBe(false);   // one probe allowed through
  });

  it('keeps keys independent, so one dead camera does not silence its siblings', () => {
    const clock = fakeClock();
    const b = createPollBackoff(POLL_BACKOFF_STEPS_MS, clock.now);
    b.fail('gmnro03/RO000H');
    expect(b.blocked('gmnro03/RO000H')).toBe(true);
    expect(b.blocked('gmnro03/RO000J')).toBe(false);
    expect(b.blocked('gmn0004/RO0014')).toBe(false);
  });
});

describe('createPollBackoff reset on success', () => {
  it('clears the window immediately', () => {
    const clock = fakeClock();
    const b = createPollBackoff(POLL_BACKOFF_STEPS_MS, clock.now);
    b.fail('a');
    b.fail('a');
    expect(b.blocked('a')).toBe(true);
    b.succeed('a');
    expect(b.blocked('a')).toBe(false);
    expect(b.size()).toBe(0);
  });

  it('restarts the ramp from the first step after a recovery', () => {
    const clock = fakeClock();
    const b = createPollBackoff(POLL_BACKOFF_STEPS_MS, clock.now);
    b.fail('a'); b.fail('a'); b.fail('a');
    b.succeed('a');
    expect(b.fail('a')).toBe(30 * S);
  });

  it('an online station never accumulates state, so its cadence is untouched', () => {
    const clock = fakeClock();
    const b = createPollBackoff(POLL_BACKOFF_STEPS_MS, clock.now);
    for (let i = 0; i < 100; i++) {
      expect(b.blocked('up')).toBe(false);
      b.succeed('up');
      clock.advance(15 * S);
    }
    expect(b.size()).toBe(0);
  });

  it('forget() drops a key we stopped polling', () => {
    const b = createPollBackoff(POLL_BACKOFF_STEPS_MS, fakeClock().now);
    b.fail('a');
    b.forget('a');
    expect(b.size()).toBe(0);
    expect(b.blocked('a')).toBe(false);
  });
});

describe('overnight request budget', () => {
  it('collapses a 15 s poll of a dead camera from ~1200 calls to ~25 over 5 h', () => {
    const clock = fakeClock();
    const b = createPollBackoff(POLL_BACKOFF_STEPS_MS, clock.now);
    const POLL_MS = 15 * S;
    const WINDOW_MS = 5 * 60 * MIN;
    let attempts = 0;
    for (let t = 0; t < WINDOW_MS; t += POLL_MS) {
      if (!b.blocked('dead')) {
        attempts++;
        b.fail('dead');   // station is down: every attempt 503s
      }
      clock.advance(POLL_MS);
    }
    expect(WINDOW_MS / POLL_MS).toBe(1200);   // what it used to cost
    expect(attempts).toBeLessThan(30);
    expect(attempts).toBeGreaterThan(15);
  });
});
