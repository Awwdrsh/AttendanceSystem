import { MIN_MS, localTimeMs, parseHHMM, roundMinutes } from './time.js';
import type { DayContext, DayType, Flag, OtPolicy, OtResult, Pair } from './types.js';

export interface OtInput {
  localDate: string;
  /** Minutes the employee is expected to work (shift span minus break). */
  scheduledMin: number;
  workedMin: number;
  dayType: DayType;
  pairs: readonly Pair[];
  ctx: DayContext;
  policy: OtPolicy;
}

export const NO_OT = (dayType: DayType, multiplier: number, rawMinutes = 0): OtResult => ({
  status: 'None', minutes: 0, rawMinutes, multiplier, dayType, nightMinutes: 0, nightPremiumRate: 0,
});

/** plan §10.4. Pushes any OT flags onto `flags`. */
export function computeOt(input: OtInput, flags: Flag[]): OtResult {
  const { policy, ctx, dayType } = input;
  const multiplier = dayType === 'holiday' ? policy.multiplierHoliday
    : dayType === 'weekly_off' ? policy.multiplierWeeklyOff : policy.multiplierWorking;

  if (!ctx.otEligible) return NO_OT(dayType, multiplier);

  // On an off day every worked minute is beyond schedule.
  let raw = dayType === 'working' ? Math.max(0, input.workedMin - input.scheduledMin) : input.workedMin;
  if (raw < policy.minThresholdMin) return NO_OT(dayType, multiplier, raw);
  raw = roundMinutes(raw, policy.roundingMin, 'down');

  if (policy.requirePreapproval) {
    const approved = ctx.otPreapprovedMin ?? 0;
    if (approved <= 0) {
      flags.push({ kind: 'OTPreapprovalMissing', rawMinutes: raw });
      return NO_OT(dayType, multiplier, raw);
    }
    raw = Math.min(raw, approved);
  }

  const capWeek = Math.max(0, policy.capWeekMin - ctx.otMinutesThisWeek);
  const minutes = Math.min(raw, policy.capDayMin, capWeek);
  if (raw > minutes) flags.push({ kind: 'OTCapExceeded', minutes: raw - minutes });

  let nightMinutes = 0;
  if (policy.nightWindowEnabled) {
    const nStart = localTimeMs(input.localDate, policy.nightWindowStart);
    const nEnd = parseHHMM(policy.nightWindowEnd) <= parseHHMM(policy.nightWindowStart)
      ? localTimeMs(input.localDate, policy.nightWindowEnd, 1)
      : localTimeMs(input.localDate, policy.nightWindowEnd);
    for (const p of input.pairs) {
      const overlap = Math.min(p.outMs, nEnd) - Math.max(p.inMs, nStart);
      if (overlap > 0) nightMinutes += Math.floor(overlap / MIN_MS);
    }
  }

  return {
    status: minutes > 0 ? 'Detected' : 'None',
    minutes, rawMinutes: raw, multiplier, dayType, nightMinutes,
    nightPremiumRate: policy.nightWindowEnabled ? policy.nightPremiumRate : 0,
  };
}
