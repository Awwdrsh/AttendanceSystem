import { describe, expect, it } from 'vitest';
import { computeDay } from './compute-day.js';
import { localTimeMs } from './time.js';
import { DEFAULT_POLICY, type DayContext, type Punch, type Shift, type AttendancePolicy } from './types.js';

const D = '2026-09-21'; // Monday (BS 2083-06-05)

const general: Shift = {
  code: 'GEN', start: '10:00', end: '18:00', graceInMin: 15, graceOutMin: 0,
  earlyInWindowMin: 60, lateOutWindowMin: 120, breakMinutes: 0,
  halfDayThresholdMin: 240, absentThresholdMin: 120, pairingMode: 'alternating',
  roundingMin: 0, roundingDirection: 'nearest',
};

const night: Shift = {
  ...general, code: 'NIGHT', start: '22:00', end: '06:00', graceInMin: 0, breakMinutes: 30,
};

const ctx = (over: Partial<DayContext> = {}): DayContext => ({
  holiday: false, weeklyOff: false, leave: 'none', wfh: false, otEligible: true,
  otPreapprovedMin: null, otMinutesThisWeek: 0, ...over,
});

let seq = 0;
const punch = (date: string, hhmm: string, dayOffset = 0, direction: Punch['direction'] = 'unknown'): Punch => ({
  id: `p${++seq}`, at: localTimeMs(date, hhmm, dayOffset), direction, verifyMode: 'fingerprint', source: 'zkteco',
});

const run = (shift: Shift | null, punches: Punch[], c = ctx(), policy: AttendancePolicy = DEFAULT_POLICY) =>
  computeDay({ employeeId: 'E1', localDate: D, shift, punches, ctx: c, policy });

describe('worked examples from plan §10.5', () => {
  it('A: late 2 min, sub-threshold OT, Present', () => {
    const day = run(general, [punch(D, '10:17'), punch(D, '18:40')]);
    expect(day.status).toBe('Present');
    expect(day.workedMin).toBe(503);
    expect(day.lateMin).toBe(2);
    expect(day.earlyMin).toBe(0);
    expect(day.ot.minutes).toBe(0);
    expect(day.ot.rawMinutes).toBe(23);
    expect(day.flags).toEqual([{ kind: 'Late', minutes: 2 }]);
  });

  it('B: night shift across midnight with fixed break is attributed to the start date', () => {
    const punches = [punch(D, '21:50'), punch(D, '06:20', 1)];
    const day = run(night, punches);
    expect(day.status).toBe('Present');
    expect(day.workedMin).toBe(480);
    expect(day.breakMin).toBe(30);
    expect(day.lateMin).toBe(0);
    expect(day.earlyMin).toBe(0);
    expect(day.ot.minutes).toBe(0);
    expect(day.punchIds).toEqual(punches.map((p) => p.id));

    // The same punches do not belong to the next day's night shift.
    const next = computeDay({ employeeId: 'E1', localDate: '2026-09-22', shift: night, punches, ctx: ctx(), policy: DEFAULT_POLICY });
    expect(next.status).toBe('Absent');
    expect(next.punchIds).toEqual([]);
  });

  it('C (day cap): 5 h approved OT is truncated to 4 h with OTCapExceeded(60)', () => {
    const shift = { ...general, lateOutWindowMin: 360 };
    const day = run(shift, [punch(D, '10:00'), punch(D, '23:00')], ctx({ otPreapprovedMin: 300 }));
    expect(day.workedMin).toBe(780);
    expect(day.ot.rawMinutes).toBe(300);
    expect(day.ot.minutes).toBe(240);
    expect(day.ot.multiplier).toBe(1.5);
    expect(day.flags).toContainEqual({ kind: 'OTCapExceeded', minutes: 60 });
  });

  it('D (weekly cap): after 24 h this week the next day\'s OT is 0 and flagged', () => {
    const shift = { ...general, lateOutWindowMin: 360 };
    const day = run(shift, [punch(D, '10:00'), punch(D, '22:00')], ctx({ otPreapprovedMin: 240, otMinutesThisWeek: 1440 }));
    expect(day.ot.minutes).toBe(0);
    expect(day.flags).toContainEqual({ kind: 'OTCapExceeded', minutes: 240 });
  });
});

describe('status precedence and flags', () => {
  it('OffShift when no shift resolves', () => {
    expect(run(null, [punch(D, '10:00')]).status).toBe('OffShift');
  });
  it('a single punch is Incomplete, never Present', () => {
    const day = run(general, [punch(D, '10:00')]);
    expect(day.status).toBe('Incomplete');
    expect(day.flags).toContainEqual({ kind: 'MissingOut' });
    expect(day.lastOutMs).toBeNull();
  });
  it('no punches is Absent; WFH overrides Absent', () => {
    expect(run(general, []).status).toBe('Absent');
    expect(run(general, [], ctx({ wfh: true })).status).toBe('WFH');
  });
  it('thresholds: below absent -> Absent, below half-day -> HalfDay', () => {
    expect(run(general, [punch(D, '10:00'), punch(D, '11:30')]).status).toBe('Absent');
    expect(run(general, [punch(D, '10:00'), punch(D, '13:00')]).status).toBe('HalfDay');
  });
  it('half-day leave on a Present day -> HalfDayLeave', () => {
    expect(run(general, [punch(D, '10:00'), punch(D, '18:00')], ctx({ leave: 'first_half' })).status).toBe('HalfDayLeave');
  });
  it('full leave wins over punches', () => {
    const day = run(general, [punch(D, '10:00'), punch(D, '18:00')], ctx({ leave: 'full' }));
    expect(day.status).toBe('OnLeave');
    expect(day.lateMin).toBe(0);
  });
  it('holiday with punches is Holiday + HolidayWork, with OT on the whole worked time', () => {
    const day = run(general, [punch(D, '10:00'), punch(D, '14:00')], ctx({ holiday: true, otPreapprovedMin: 240 }));
    expect(day.status).toBe('Holiday');
    expect(day.flags).toContainEqual({ kind: 'HolidayWork', workedMin: 240 });
    expect(day.ot).toMatchObject({ minutes: 240, dayType: 'holiday' });
  });
  it('weekly off without punches is WeeklyOff, not Absent', () => {
    expect(run(general, [], ctx({ weeklyOff: true })).status).toBe('WeeklyOff');
  });
  it('OT without pre-approval is flagged and not paid', () => {
    const day = run(general, [punch(D, '10:00'), punch(D, '19:00')]);
    expect(day.ot.minutes).toBe(0);
    expect(day.flags).toContainEqual({ kind: 'OTPreapprovalMissing', rawMinutes: 60 });
  });
  it('OT ineligible grade never gets OT', () => {
    const day = run(general, [punch(D, '10:00'), punch(D, '19:00')], ctx({ otEligible: false, otPreapprovedMin: 60 }));
    expect(day.ot.minutes).toBe(0);
    expect(day.flags.some((f) => f.kind.startsWith('OT'))).toBe(false);
  });
  it('early out is a flag with minutes', () => {
    const day = run(general, [punch(D, '10:00'), punch(D, '17:30')]);
    expect(day.earlyMin).toBe(30);
    expect(day.flags).toContainEqual({ kind: 'EarlyOut', minutes: 30 });
  });
});

describe('pairing modes', () => {
  it('alternating with four punches excludes the measured break', () => {
    const day = run(general, [punch(D, '10:00'), punch(D, '13:00'), punch(D, '14:00'), punch(D, '18:00')]);
    expect(day.workedMin).toBe(420);
    expect(day.breakMin).toBe(60);
  });
  it('strict_log_type ignores a duplicate IN and keeps the first', () => {
    const shift = { ...general, pairingMode: 'strict_log_type' as const };
    const day = run(shift, [punch(D, '10:00', 0, 'in'), punch(D, '10:01', 0, 'in'), punch(D, '18:00', 0, 'out')]);
    expect(day.workedMin).toBe(480);
    expect(day.flags).toContainEqual({ kind: 'UnpairedPunch', punchId: day.punchIds[1] });
    expect(day.flags).not.toContainEqual({ kind: 'MissingOut' });
  });
  it('first_last spans first to last punch including breaks', () => {
    const shift = { ...general, pairingMode: 'first_last' as const, breakMinutes: 30 };
    const day = run(shift, [punch(D, '10:00'), punch(D, '13:00'), punch(D, '14:00'), punch(D, '18:00')]);
    expect(day.workedMin).toBe(450);
  });
  it('rounding applies to worked minutes', () => {
    const shift = { ...general, roundingMin: 15, roundingDirection: 'down' as const };
    expect(run(shift, [punch(D, '10:00'), punch(D, '18:14')]).workedMin).toBe(480);
  });
});

describe('inputs hash', () => {
  it('is stable across calls and ignores off-window punches', () => {
    const inWindow = [punch(D, '10:00'), punch(D, '18:00')];
    const a = run(general, inWindow);
    const b = run(general, [...inWindow, punch(D, '03:00'), punch(D, '23:30')]);
    expect(a.inputsHash).toBe(b.inputsHash);
    expect(a).toEqual(b);
  });
  it('changes when a punch, the shift or the policy version changes', () => {
    const base = run(general, [punch(D, '10:00'), punch(D, '18:00')]);
    expect(run(general, [punch(D, '10:00'), punch(D, '18:01')]).inputsHash).not.toBe(base.inputsHash);
    expect(run({ ...general, graceInMin: 10 }, [punch(D, '10:00'), punch(D, '18:00')]).inputsHash).not.toBe(base.inputsHash);
    expect(run(general, [punch(D, '10:00'), punch(D, '18:00')], ctx(), { ...DEFAULT_POLICY, version: 'v2' }).inputsHash).not.toBe(base.inputsHash);
  });
});
