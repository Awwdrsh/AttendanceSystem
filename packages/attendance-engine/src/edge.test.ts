import { describe, expect, it } from 'vitest';
import { computeDay } from './compute-day.js';
import { localTimeMs, nptToday } from './time.js';
import { DEFAULT_POLICY, type DayContext, type Punch, type Shift } from './types.js';

const D = '2026-09-21';
const general: Shift = {
  code: 'GEN', start: '10:00', end: '18:00', graceInMin: 15, graceOutMin: 0, earlyInWindowMin: 60, lateOutWindowMin: 120,
  breakMinutes: 0, halfDayThresholdMin: 240, absentThresholdMin: 120, pairingMode: 'alternating', roundingMin: 0, roundingDirection: 'nearest',
};
const night: Shift = { ...general, code: 'NIGHT', start: '22:00', end: '06:00', graceInMin: 0, breakMinutes: 30 };
const ctx = (o: Partial<DayContext> = {}): DayContext => ({ holiday: false, weeklyOff: false, leave: 'none', wfh: false, otEligible: true, otPreapprovedMin: null, otMinutesThisWeek: 0, ...o });
let n = 0;
const p = (date: string, hhmm: string, off = 0, direction: Punch['direction'] = 'unknown', source = 'zkteco', id?: string): Punch =>
  ({ id: id ?? `p${++n}`, at: localTimeMs(date, hhmm, off), direction, verifyMode: 'fingerprint', source });
const run = (shift: Shift | null, punches: Punch[], c = ctx(), localDate = D) => computeDay({ employeeId: 'E', localDate, shift, punches, ctx: c, policy: DEFAULT_POLICY });

describe('window boundaries', () => {
  it('punches exactly at the window edges are included; one millisecond outside is excluded', () => {
    // The late-out edge is max(configured 120 min, OT cap 240 + 120) = 6 h after the 18:00 end, i.e. midnight.
    const start = p(D, '09:00'); const end = p(D, '00:00', 1);
    const day = run(general, [start, end]);
    expect(day.punchIds).toEqual([start.id, end.id]);
    const before = { ...p(D, '09:00'), at: localTimeMs(D, '09:00') - 1 };
    const after = { ...p(D, '00:00', 1), at: localTimeMs(D, '00:00', 1) + 1 };
    expect(run(general, [before, after]).punchIds).toEqual([]);
  });
  it('a night shift OUT from the previous day never lands in the next day of the same shift', () => {
    const punches = [p('2026-09-20', '21:55'), p('2026-09-21', '06:10')];
    expect(run(night, punches, ctx(), '2026-09-20').status).toBe('Present');
    const next = run(night, punches, ctx(), '2026-09-21');
    expect(next.status).toBe('Absent');
    expect(next.punchIds).toEqual([]);
  });
  it('shift with start == end is treated as a 24 h shift crossing midnight', () => {
    const day = run({ ...general, start: '08:00', end: '08:00' }, [p(D, '08:00'), p(D, '08:00', 1)]);
    expect(day.scheduledMin).toBe(1440);
    expect(day.workedMin).toBe(1440);
    expect(day.status).toBe('Present');
  });
  it('identical timestamps are ordered by id so recomputes are stable', () => {
    const a = p(D, '10:00', 0, 'unknown', 'zkteco', 'b'); const b = { ...p(D, '10:00', 0, 'unknown', 'zkteco', 'a') };
    const out = p(D, '18:00');
    const d1 = run(general, [a, b, out]); const d2 = run(general, [b, out, a]);
    expect(d1).toEqual(d2);
    expect(d1.punchIds).toEqual(['a', 'b', out.id]);
  });
});

describe('thresholds and modes', () => {
  it('zero thresholds disable half-day and absent classification', () => {
    const d = run({ ...general, halfDayThresholdMin: 0, absentThresholdMin: 0 }, [p(D, '10:00'), p(D, '10:30')]);
    expect(d.status).toBe('Present');
    expect(d.workedMin).toBe(30);
  });
  it('strict mode with only unknown directions falls back to alternating roles', () => {
    const d = run({ ...general, pairingMode: 'strict_log_type' }, [p(D, '10:00'), p(D, '13:00'), p(D, '14:00'), p(D, '18:00')]);
    expect(d.workedMin).toBe(420);
    expect(d.pairs).toHaveLength(2);
  });
  it('first_last with three punches ignores the middle one and has no MissingOut', () => {
    const d = run({ ...general, pairingMode: 'first_last' }, [p(D, '10:00'), p(D, '13:00'), p(D, '18:00')]);
    expect(d.workedMin).toBe(480);
    expect(d.flags.some((f) => f.kind === 'MissingOut' || f.kind === 'UnpairedPunch')).toBe(false);
  });
});

describe('leave, holiday and off-day interplay', () => {
  it('half-day leave with no punches stays Absent but carries a HalfDayLeave flag so payroll counts half a day', () => {
    const d = run(general, [], ctx({ leave: 'first_half' }));
    expect(d.status).toBe('Absent');
    expect(d.flags).toContainEqual({ kind: 'HalfDayLeave', half: 'first_half' });
  });
  it('half-day leave with a present other half is HalfDayLeave with the flag', () => {
    const d = run(general, [p(D, '14:00'), p(D, '18:00')], ctx({ leave: 'second_half' }));
    expect(d.status).toBe('HalfDayLeave');
    expect(d.flags).toContainEqual({ kind: 'HalfDayLeave', half: 'second_half' });
  });
  it('holiday wins over approved leave: no leave day is consumed on a holiday', () => {
    const d = run(general, [], ctx({ holiday: true, leave: 'full' }));
    expect(d.status).toBe('Holiday');
  });
  it('OT on a weekly off without pre-approval is flagged, not paid', () => {
    const d = run(general, [p(D, '10:00'), p(D, '14:00')], ctx({ weeklyOff: true }));
    expect(d.status).toBe('WeeklyOff');
    expect(d.ot.minutes).toBe(0);
    expect(d.flags).toContainEqual({ kind: 'HolidayWork', workedMin: 240 });
    expect(d.flags).toContainEqual({ kind: 'OTPreapprovalMissing', rawMinutes: 240 });
  });
  it('an automatic clock-out punch marks the day so HR can see it was not a real OUT', () => {
    const d = run(general, [p(D, '10:00'), p(D, '18:00', 0, 'out', 'auto_clock_out')]);
    expect(d.status).toBe('Present');
    expect(d.flags).toContainEqual({ kind: 'AutoClockOut' });
  });
});

describe('nptToday', () => {
  it('is the Nepal calendar date, not the UTC date, in the 00:00-05:45 NPT window', () => {
    expect(nptToday(Date.UTC(2026, 8, 21, 18, 30))).toBe('2026-09-22'); // 00:15 NPT on the 22nd
    expect(nptToday(Date.UTC(2026, 8, 21, 18, 14))).toBe('2026-09-21'); // 23:59 NPT on the 21st
  });
});
