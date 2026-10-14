import { describe, expect, it } from 'vitest';
import { computeDay } from './compute-day.js';
import { localTimeMs } from './time.js';
import { DEFAULT_POLICY, type DayContext, type Punch, type Shift } from './types.js';

const D = '2026-09-21';
const general: Shift = {
  code: 'GEN', start: '09:00', end: '18:00', graceInMin: 15, graceOutMin: 0, earlyInWindowMin: 60, lateOutWindowMin: 120,
  breakMinutes: 60, halfDayThresholdMin: 240, absentThresholdMin: 120, pairingMode: 'alternating', roundingMin: 0, roundingDirection: 'nearest',
};
const night: Shift = { ...general, code: 'NIGHT', start: '22:00', end: '06:00', graceInMin: 0, breakMinutes: 30 };
const ctx = (over: Partial<DayContext> = {}): DayContext => ({
  holiday: false, weeklyOff: false, leave: 'none', wfh: false, otEligible: true, otPreapprovedMin: 600, otMinutesThisWeek: 0, ...over,
});
let seq = 0;
const punch = (date: string, hhmm: string, dayOffset = 0): Punch => ({ id: `w${++seq}`, at: localTimeMs(date, hhmm, dayOffset), direction: 'unknown', verifyMode: 'fingerprint', source: 'zkteco' });
const run = (shift: Shift, punches: Punch[], date = D) => computeDay({ employeeId: 'E1', localDate: date, shift, punches, ctx: ctx(), policy: DEFAULT_POLICY });

describe('overtime punch-outs beyond the configured late-out window', () => {
  it('an OUT 2.5 h after shift end still pairs (window reaches past the 4 h OT cap) and is Present with OT', () => {
    const day = run(general, [punch(D, '09:00'), punch(D, '20:30')]);
    expect(day.status).toBe('Present');
    expect(day.flags.map((f) => f.kind)).not.toContain('MissingOut');
    expect(day.workedMin).toBe(630);           // 11.5 h less the 1 h break
    expect(day.ot.rawMinutes).toBe(150);       // like with like: 630 worked vs 480 expected
  });
  it('a night shift OUT 3.5 h late pairs across midnight and counts the full overtime', () => {
    const day = run(night, [punch(D, '21:58'), punch(D, '09:30', 1)]);
    expect(day.status).toBe('Present');
    expect(day.ot.rawMinutes).toBe(210 - 0);   // 11 h 32 m − 30 m break = 662 worked vs 450 expected → 212, rounded down to 15 → 210
  });
  it('the widened window never swallows the next day\'s punches', () => {
    const next = '2026-09-22';
    const punches = [punch(D, '09:00'), punch(D, '18:00'), punch(next, '08:30'), punch(next, '18:00', 0)];
    expect(run(general, punches).punchIds).toHaveLength(2);
    expect(run(general, punches, next).punchIds).toHaveLength(2);
  });
});

describe('overtime minutes compare worked time with scheduled time less the break', () => {
  it('30 min break, OUT 90 min late → 90 min of OT, not 60', () => {
    const day = run({ ...general, breakMinutes: 30 }, [punch(D, '09:00'), punch(D, '19:30')]);
    expect(day.ot.rawMinutes).toBe(90);
  });
  it('no OT when the employee works exactly the shift less the break', () => {
    const day = run(general, [punch(D, '09:00'), punch(D, '18:00')]);
    expect(day.ot.rawMinutes).toBe(0);
  });
});
