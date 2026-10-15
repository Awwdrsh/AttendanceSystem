import { describe, expect, it } from 'vitest';
import { computeDay } from './compute-day.js';
import { localTimeMs } from './time.js';
import { DEFAULT_POLICY, type DayContext, type Punch, type Shift } from './types.js';

const D = '2026-09-21';
const general: Shift = {
  code: 'GEN', start: '10:00', end: '18:00', graceInMin: 15, graceOutMin: 0,
  earlyInWindowMin: 60, lateOutWindowMin: 120, breakMinutes: 0,
  halfDayThresholdMin: 240, absentThresholdMin: 120, pairingMode: 'alternating',
  roundingMin: 0, roundingDirection: 'nearest',
};
const night: Shift = { ...general, code: 'NIGHT', start: '22:00', end: '06:00', graceInMin: 0 };

const ctx = (over: Partial<DayContext> = {}): DayContext => ({
  holiday: false, weeklyOff: false, leave: 'none', wfh: false, otEligible: true, otPreapprovedMin: null, otMinutesThisWeek: 0, ...over,
});
let seq = 0;
const punch = (hhmm: string, dayOffset = 0): Punch => ({ id: `s${++seq}`, at: localTimeMs(D, hhmm, dayOffset), direction: 'unknown', verifyMode: 'fp', source: 'zkteco' });
const run = (shift: Shift, punches: Punch[], c: DayContext) => computeDay({ employeeId: 'E1', localDate: D, shift, punches, ctx: c, policy: DEFAULT_POLICY });

describe('hourly (short) leave', () => {
  it('a morning window excuses the late arrival', () => {
    const without = run(general, [punch('12:05'), punch('18:00')], ctx());
    expect(without.lateMin).toBe(110);
    const day = run(general, [punch('12:05'), punch('18:00')], ctx({ shortLeave: [{ from: '10:00', to: '12:00' }] }));
    expect(day.lateMin).toBe(0);
    expect(day.status).toBe('Present');
    expect(day.flags).toContainEqual({ kind: 'ShortLeave', minutes: 120 });
  });

  it('arriving after the window plus grace is still late, counted from the end of the window', () => {
    const day = run(general, [punch('12:30'), punch('18:00')], ctx({ shortLeave: [{ from: '10:00', to: '12:00' }] }));
    expect(day.lateMin).toBe(15);
  });

  it('an afternoon window excuses leaving early', () => {
    const day = run(general, [punch('10:00'), punch('16:00')], ctx({ shortLeave: [{ from: '16:00', to: '18:00' }] }));
    expect(day.earlyMin).toBe(0);
    const partly = run(general, [punch('10:00'), punch('15:30')], ctx({ shortLeave: [{ from: '16:00', to: '18:00' }] }));
    expect(partly.earlyMin).toBe(30);
  });

  it('leave minutes count toward the half-day threshold', () => {
    // 3 h worked + 2 h leave = 5 h credited, above the 4 h half-day threshold.
    const without = run(general, [punch('13:00'), punch('16:00')], ctx());
    expect(without.status).toBe('HalfDay');
    const day = run(general, [punch('13:00'), punch('16:00')], ctx({ shortLeave: [{ from: '16:00', to: '18:00' }] }));
    expect(day.status).toBe('Present');
    expect(day.workedMin).toBe(180);
  });

  it('a mid-day window does not move arrival or departure and overlapping work is not double counted', () => {
    const day = run(general, [punch('10:00'), punch('14:00'), punch('15:00'), punch('15:30')], ctx({ shortLeave: [{ from: '13:00', to: '15:00' }] }));
    expect(day.earlyMin).toBe(150);
    // worked 270 min; leave 13:00-15:00 overlaps 60 min of work, so 60 leave minutes are credited.
    expect(day.status).toBe('Present');
  });

  it('a day with leave but no punches is still absent', () => {
    expect(run(general, [], ctx({ shortLeave: [{ from: '10:00', to: '12:00' }] })).status).toBe('Absent');
  });

  it('works across midnight on a night shift', () => {
    const day = run(night, [punch('22:00'), punch('04:00', 1)], ctx({ shortLeave: [{ from: '04:00', to: '06:00' }] }));
    expect(day.earlyMin).toBe(0);
    expect(day.flags).toContainEqual({ kind: 'ShortLeave', minutes: 120 });
  });

  it('no short leave leaves the inputs hash unchanged; a short leave changes it', () => {
    const ps = [punch('10:00'), punch('18:00')];
    const plain = run(general, ps, ctx());
    expect(run(general, ps, { ...ctx(), shortLeave: undefined }).inputsHash).toBe(plain.inputsHash);
    expect(run(general, ps, ctx({ shortLeave: [{ from: '10:00', to: '11:00' }] })).inputsHash).not.toBe(plain.inputsHash);
  });
});
