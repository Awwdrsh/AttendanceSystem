import { canonicalJson, sha256Hex } from './hash.js';
import { computeOt, NO_OT } from './ot.js';
import { pairPunches } from './pairing.js';
import { DAY_MIN, MIN_MS, localMidnightMs, minutesBetween, parseHHMM, roundMinutes } from './time.js';
import type { AttendanceDay, AttendanceStatus, ComputeDayInput, DayType, Flag, Punch } from './types.js';

/**
 * Pure function: (employee, date, shift, punches, context, policy) -> AttendanceDay.
 * No I/O, no clock. Same inputs always give the same output and the same inputsHash (plan §10.1-10.3).
 */
export function computeDay(input: ComputeDayInput): AttendanceDay {
  const { employeeId, localDate, shift, ctx, policy } = input;
  const flags: Flag[] = [];

  if (!shift) {
    return finalize({
      employeeId, localDate, shiftCode: null, status: 'OffShift', window: null, scheduledMin: 0,
      firstInMs: null, lastOutMs: null, workedMin: 0, breakMin: 0, lateMin: 0, earlyMin: 0,
      ot: NO_OT('working', policy.ot.multiplierWorking), flags, pairs: [], punchIds: [],
    }, [], input);
  }

  const midnight = localMidnightMs(localDate);
  const startMin = parseHHMM(shift.start);
  const endMinRaw = parseHHMM(shift.end);
  const crossesMidnight = endMinRaw <= startMin;
  const startMs = midnight + startMin * MIN_MS;
  const endMs = midnight + (endMinRaw + (crossesMidnight ? DAY_MIN : 0)) * MIN_MS;
  const scheduledMin = (endMs - startMs) / MIN_MS;
  const window = { startMs: startMs - shift.earlyInWindowMin * MIN_MS, endMs: endMs + shift.lateOutWindowMin * MIN_MS };

  // Only punches inside the window belong to this day; everything else is off-shift for this date.
  const punches = input.punches
    .filter((p) => p.at >= window.startMs && p.at <= window.endMs)
    .sort((a, b) => a.at - b.at || a.id.localeCompare(b.id));

  const { pairs, unpaired } = pairPunches(punches, shift.pairingMode);

  let gross = 0;
  let breakMin = 0;
  if (pairs.length > 0) {
    gross = pairs.reduce((sum, p) => sum + minutesBetween(p.inMs, p.outMs), 0);
    if (pairs.length === 1) {
      breakMin = Math.min(shift.breakMinutes, gross);
    } else {
      for (let i = 1; i < pairs.length; i++) breakMin += minutesBetween(pairs[i - 1]!.outMs, pairs[i]!.inMs);
    }
  }
  // With one pair the fixed break is deducted; with several pairs the gaps are already excluded from gross.
  const net = pairs.length === 1 ? gross - breakMin : gross;
  const workedMin = roundMinutes(net, shift.roundingMin, shift.roundingDirection);

  const firstInMs = pairs[0]?.inMs ?? punches[0]?.at ?? null;
  const lastOutMs = pairs.length > 0 ? pairs[pairs.length - 1]!.outMs : null;

  const dayType: DayType = ctx.holiday ? 'holiday' : ctx.weeklyOff ? 'weekly_off' : 'working';
  let status: AttendanceStatus;
  let lateMin = 0;
  let earlyMin = 0;
  let ot = NO_OT(dayType, policy.ot.multiplierWorking);

  if (dayType !== 'working') {
    // Holiday/WeeklyOff > everything. Never Absent on an off day (plan §10.7).
    status = ctx.holiday ? 'Holiday' : 'WeeklyOff';
    if (punches.length > 0) {
      flags.push({ kind: 'HolidayWork', workedMin });
      ot = computeOt({ localDate, scheduledMin, workedMin, dayType, pairs, ctx, policy: policy.ot }, flags);
    }
  } else if (ctx.leave === 'full') {
    status = 'OnLeave';
  } else {
    if (firstInMs !== null) lateMin = Math.max(0, minutesBetween(startMs + shift.graceInMin * MIN_MS, firstInMs));
    if (lastOutMs !== null) earlyMin = Math.max(0, minutesBetween(lastOutMs, endMs - shift.graceOutMin * MIN_MS));

    if (punches.length === 0) status = 'Absent';
    else if (pairs.length === 0) status = 'Incomplete';
    else if (shift.absentThresholdMin > 0 && workedMin < shift.absentThresholdMin) status = 'Absent';
    else if (shift.halfDayThresholdMin > 0 && workedMin < shift.halfDayThresholdMin) status = 'HalfDay';
    else status = 'Present';

    if ((ctx.leave === 'first_half' || ctx.leave === 'second_half') && status === 'Present') status = 'HalfDayLeave';
    if (ctx.wfh && (status === 'Absent' || status === 'Incomplete')) status = 'WFH';

    if (lateMin > 0) flags.push({ kind: 'Late', minutes: lateMin });
    if (earlyMin > 0) flags.push({ kind: 'EarlyOut', minutes: earlyMin });
    ot = computeOt({ localDate, scheduledMin, workedMin, dayType, pairs, ctx, policy: policy.ot }, flags);
  }

  if (dayType === 'working' && (ctx.leave === 'first_half' || ctx.leave === 'second_half')) flags.push({ kind: 'HalfDayLeave', half: ctx.leave });
  if (punches.some((p) => p.source === 'auto_clock_out')) flags.push({ kind: 'AutoClockOut' });
  for (const p of unpaired) flags.push({ kind: 'UnpairedPunch', punchId: p.id });
  const lastUnpaired = unpaired[unpaired.length - 1];
  if (lastUnpaired && punches.length > 0 && lastUnpaired.id === punches[punches.length - 1]!.id && lastUnpaired.direction !== 'out') {
    flags.push({ kind: 'MissingOut' });
  }

  return finalize({
    employeeId, localDate, shiftCode: shift.code, status, window, scheduledMin,
    firstInMs, lastOutMs, workedMin, breakMin, lateMin, earlyMin, ot, flags, pairs,
    punchIds: punches.map((p) => p.id),
  }, punches, input);
}

function finalize(day: Omit<AttendanceDay, 'inputsHash'>, punches: readonly Punch[], input: ComputeDayInput): AttendanceDay {
  const inputsHash = sha256Hex(canonicalJson({
    employeeId: input.employeeId,
    localDate: input.localDate,
    shift: input.shift,
    ctx: input.ctx,
    policy: input.policy,
    punches: punches.map((p) => ({ id: p.id, at: p.at, direction: p.direction, verifyMode: p.verifyMode, source: p.source })),
  }));
  return { ...day, inputsHash };
}
