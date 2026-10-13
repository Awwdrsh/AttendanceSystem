import { describe, expect, it } from 'vitest';
import fc from 'fast-check';
import { computeDay } from './compute-day.js';
import { localMidnightMs, MIN_MS, DAY_MIN } from './time.js';
import { DEFAULT_POLICY, type DayContext, type Punch, type Shift } from './types.js';

// Invariants from plan §10.3 / §15.2.
const D = '2026-09-21';
const midnight = localMidnightMs(D);
const hhmm = (min: number) => `${String(Math.floor(min / 60)).padStart(2, '0')}:${String(min % 60).padStart(2, '0')}`;

const shiftArb: fc.Arbitrary<Shift> = fc.record({
  code: fc.constant('S'),
  start: fc.integer({ min: 0, max: DAY_MIN - 1 }).map(hhmm),
  end: fc.integer({ min: 0, max: DAY_MIN - 1 }).map(hhmm),
  graceInMin: fc.integer({ min: 0, max: 60 }),
  graceOutMin: fc.integer({ min: 0, max: 60 }),
  earlyInWindowMin: fc.integer({ min: 0, max: 180 }),
  lateOutWindowMin: fc.integer({ min: 0, max: 360 }),
  breakMinutes: fc.integer({ min: 0, max: 90 }),
  halfDayThresholdMin: fc.integer({ min: 0, max: 300 }),
  absentThresholdMin: fc.integer({ min: 0, max: 180 }),
  pairingMode: fc.constantFrom('alternating', 'strict_log_type', 'first_last'),
  roundingMin: fc.constantFrom(0, 5, 15),
  roundingDirection: fc.constantFrom('nearest', 'down', 'up'),
});

const punchArb = (idPrefix: string): fc.Arbitrary<Punch> => fc.record({
  id: fc.uuid().map((u) => `${idPrefix}${u}`),
  // Anywhere from one day before to two days after local midnight, with second-level jitter.
  at: fc.integer({ min: -DAY_MIN * 60, max: 2 * DAY_MIN * 60 }).map((s) => midnight + s * 1000),
  direction: fc.constantFrom('in', 'out', 'unknown'),
  verifyMode: fc.constantFrom('fingerprint', 'face', 'card'),
  source: fc.constant('zkteco'),
});

const ctxArb: fc.Arbitrary<DayContext> = fc.record({
  holiday: fc.boolean(),
  weeklyOff: fc.boolean(),
  leave: fc.constantFrom('none', 'full', 'first_half', 'second_half'),
  wfh: fc.boolean(),
  otEligible: fc.boolean(),
  otPreapprovedMin: fc.option(fc.integer({ min: 0, max: 600 }), { nil: null }),
  otMinutesThisWeek: fc.integer({ min: 0, max: 1440 }),
});

const inputArb = fc.record({
  shift: shiftArb,
  punches: fc.array(punchArb('p'), { maxLength: 12 }),
  ctx: ctxArb,
});

const compute = (shift: Shift, punches: Punch[], ctx: DayContext) =>
  computeDay({ employeeId: 'E', localDate: D, shift, punches, ctx, policy: DEFAULT_POLICY });

describe('attendance engine invariants', () => {
  it('no negative durations and worked never exceeds the window', () => {
    fc.assert(fc.property(inputArb, ({ shift, punches, ctx }) => {
      const day = compute(shift, punches, ctx);
      const windowMin = (day.window!.endMs - day.window!.startMs) / MIN_MS;
      expect(day.workedMin).toBeGreaterThanOrEqual(0);
      expect(day.breakMin).toBeGreaterThanOrEqual(0);
      expect(day.lateMin).toBeGreaterThanOrEqual(0);
      expect(day.earlyMin).toBeGreaterThanOrEqual(0);
      expect(day.ot.minutes).toBeGreaterThanOrEqual(0);
      expect(day.ot.minutes).toBeLessThanOrEqual(DEFAULT_POLICY.ot.capDayMin);
      // Rounding up by at most one step may push worked slightly past the window.
      expect(day.workedMin).toBeLessThanOrEqual(windowMin + shift.roundingMin);
    }), { numRuns: 300 });
  });

  it('recompute is idempotent', () => {
    fc.assert(fc.property(inputArb, ({ shift, punches, ctx }) => {
      expect(compute(shift, punches, ctx)).toEqual(compute(shift, [...punches].reverse(), ctx));
    }), { numRuns: 200 });
  });

  it('adding a punch outside the window never changes the day', () => {
    fc.assert(fc.property(inputArb, punchArb('x'), ({ shift, punches, ctx }, extra) => {
      const base = compute(shift, punches, ctx);
      const w = base.window!;
      const outside = { ...extra, at: extra.at <= w.endMs ? w.endMs + 60_000 + (extra.at % 7) * 1000 : extra.at };
      expect(compute(shift, [...punches, outside], ctx)).toEqual(base);
    }), { numRuns: 200 });
  });

  it('a single in-window punch is never Present', () => {
    fc.assert(fc.property(shiftArb, punchArb('p'), (shift, p) => {
      const w = computeDay({ employeeId: 'E', localDate: D, shift, punches: [], ctx: ctxWorking, policy: DEFAULT_POLICY }).window!;
      const inside = { ...p, at: w.startMs + Math.abs(p.at % (w.endMs - w.startMs + 1)) };
      const day = compute(shift, [inside], ctxWorking);
      expect(day.status).not.toBe('Present');
      expect(['Incomplete', 'WFH', 'Absent']).toContain(day.status);
    }), { numRuns: 200 });
  });
});

const ctxWorking: DayContext = {
  holiday: false, weeklyOff: false, leave: 'none', wfh: false, otEligible: true, otPreapprovedMin: null, otMinutesThisWeek: 0,
};
