import { z } from 'zod';
import { createHash } from 'node:crypto';

const hhmm = z.string().regex(/^([01]\d|2[0-3]):[0-5]\d$/);

/** Statutory maxima (Labour Act 2074 Sec 30(1)) can be lowered by policy, never raised. */
export const OT_CAP_DAY_MIN = 240;
export const OT_CAP_WEEK_MIN = 1440;

export const settingSchemas = {
  'attendance.policy': z.object({
    version: z.string().optional(),
    otMinThresholdMin: z.number().int().min(0).max(240).default(30),
    otRoundingMin: z.number().int().min(0).max(60).default(15),
    requirePreapproval: z.boolean().default(true),
    otCapDayMin: z.number().int().min(0).default(OT_CAP_DAY_MIN).transform((v) => Math.min(v, OT_CAP_DAY_MIN)),
    otCapWeekMin: z.number().int().min(0).default(OT_CAP_WEEK_MIN).transform((v) => Math.min(v, OT_CAP_WEEK_MIN)),
    holidayWork: z.enum(['substitute_leave', 'pay', 'employee_choice']).default('substitute_leave'),
    nightWindowEnabled: z.boolean().default(false),
    nightPremiumRate: z.number().min(0).max(1).default(0),
    autoClockOutAfterMin: z.number().int().min(0).max(720).default(0),
    regularisationBackdateDays: z.number().int().min(0).max(90).default(30),
    regularisationMonthlyCap: z.number().int().min(0).max(31).default(5),
    latePenalty: z.object({ enabled: z.boolean().default(false), lateCount: z.number().int().min(1).default(3), deductionDays: z.number().min(0).max(1).default(0.5) }).default({ enabled: false, lateCount: 3, deductionDays: 0.5 }),
  }),
  'leave.policy': z.object({
    leaveYear: z.enum(['fiscal', 'anniversary']).default('fiscal'),
    sandwichRule: z.boolean().default(false),
    probationRestriction: z.boolean().default(true),
    maxContinuousDays: z.number().int().min(1).max(365).default(30),
  }),
  'payroll.policy': z.object({
    prorationBasis: z.enum(['bs_calendar_days', 'fixed_30', '26_day']).default('bs_calendar_days'),
    hourlyDivisor: z.number().int().min(150).max(260).default(208),
    payDay: z.number().int().min(1).max(32).default(25),
    rounding: z.enum(['half_up_rupee', 'paisa']).default('half_up_rupee'),
    separateApprover: z.boolean().default(false),
    encashmentDivisor: z.enum(['bs_days', 'fixed_30']).default('bs_days'),
  }),
  'payslip.policy': z.object({
    language: z.enum(['both', 'en', 'ne']).default('both'),
    numerals: z.enum(['latin', 'devanagari']).default('latin'),
    passwordProtect: z.boolean().default(true),
  }),
  'notifications.policy': z.object({
    quietHoursStart: hhmm.default('21:00'),
    quietHoursEnd: hhmm.default('07:00'),
    slaHours: z.number().int().min(1).max(168).default(24),
    escalationHours: z.number().int().min(1).max(336).default(48),
    digestTime: hhmm.default('10:45'),
  }),
  'company.profile': z.object({
    nameEn: z.string().min(1).max(120),
    nameNe: z.string().max(120).nullish(),
    address: z.string().max(300).nullish(),
    pan: z.string().regex(/^\d{9}$/).nullish(),
    ssfEmployerId: z.string().max(20).nullish(),
    logoDataUrl: z.string().max(200_000).regex(/^data:image\/(png|jpeg|svg\+xml);base64,/).nullish(),
  }),
} as const;

export type SettingKey = keyof typeof settingSchemas;
export const SETTING_KEYS = Object.keys(settingSchemas) as SettingKey[];

export const settingDescriptions: Record<SettingKey, string> = {
  'attendance.policy': 'Overtime threshold/rounding/approval, holiday work, night premium, auto clock-out, regularisation limits. Statutory OT caps (4 h/day, 24 h/week) cannot be raised.',
  'leave.policy': 'Leave year, sandwich rule, probation restriction, maximum continuous days.',
  'payroll.policy': 'Proration basis, hourly divisor, pay day, rounding, whether a different HR user must approve, encashment divisor.',
  'payslip.policy': 'Payslip language, numerals and PDF protection.',
  'notifications.policy': 'Quiet hours, SLA and escalation timing, digest time.',
  'company.profile': 'Company name, address, PAN, SSF employer ID and logo shown on payslips and the app header.',
};

/** Deterministic version tag so recompute notices policy edits (inputs_hash includes policy). */
export function policyVersion(value: Record<string, unknown>): string {
  const { version: _v, ...rest } = value;
  const sorted = JSON.stringify(rest, Object.keys(rest).sort());
  return 'v-' + createHash('sha256').update(sorted).digest('hex').slice(0, 12);
}
