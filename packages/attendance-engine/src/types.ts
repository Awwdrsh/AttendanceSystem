export type PairingMode = 'alternating' | 'strict_log_type' | 'first_last';
export type RoundingDirection = 'nearest' | 'down' | 'up';

/** Mirrors Frappe Shift Type semantics (plan §5.3, §10.1). Times are local 'HH:MM'; end <= start means the shift crosses midnight. */
export interface Shift {
  code: string;
  start: string;
  end: string;
  graceInMin: number;
  graceOutMin: number;
  earlyInWindowMin: number;
  lateOutWindowMin: number;
  /** Fixed break deduction in minutes; 0 = none. With more than one pair, gaps between pairs are the break instead. */
  breakMinutes: number;
  /** Worked minutes below this -> HalfDay. 0 disables. Applies only when at least one pair exists. */
  halfDayThresholdMin: number;
  /** Worked minutes below this -> Absent. 0 disables. */
  absentThresholdMin: number;
  pairingMode: PairingMode;
  /** Rounding step for worked minutes; 0 = none. */
  roundingMin: number;
  roundingDirection: RoundingDirection;
}

export type PunchDirection = 'in' | 'out' | 'unknown';

export interface Punch {
  id: string;
  /** Epoch ms (UTC instant). */
  at: number;
  direction: PunchDirection;
  verifyMode: string;
  source: string;
}

export type LeaveCover = 'none' | 'full' | 'first_half' | 'second_half';

export interface DayContext {
  holiday: boolean;
  weeklyOff: boolean;
  leave: LeaveCover;
  /** Approved WFH or on-duty for the day. */
  wfh: boolean;
  otEligible: boolean;
  /** Approved pre-approval minutes for the day; null = none filed. */
  otPreapprovedMin: number | null;
  /** OT minutes already counted this week on other days (for the 24 h/week cap, Sec 30(1)). */
  otMinutesThisWeek: number;
}

export type HolidayWorkPolicy = 'substitute_leave' | 'pay' | 'employee_choice';

export interface OtPolicy {
  minThresholdMin: number;
  roundingMin: number;
  requirePreapproval: boolean;
  /** Labour Act 2074 Sec 30(1): 4 h/day. */
  capDayMin: number;
  /** Sec 30(1): 24 h/week. */
  capWeekMin: number;
  /** Sec 31(1): 1.5x basic on working days. */
  multiplierWorking: number;
  multiplierWeeklyOff: number;
  multiplierHoliday: number;
  /** Non-statutory 22:00-04:00 premium; default off (plan §6.1). */
  nightWindowEnabled: boolean;
  nightWindowStart: string;
  nightWindowEnd: string;
  nightPremiumRate: number;
}

export interface AttendancePolicy {
  /** Included in inputs_hash so policy edits trigger recompute. Must change whenever a value changes. */
  version: string;
  ot: OtPolicy;
  holidayWork: HolidayWorkPolicy;
}

export const DEFAULT_POLICY: AttendancePolicy = {
  version: 'default-v1',
  ot: {
    minThresholdMin: 30,
    roundingMin: 15,
    requirePreapproval: true,
    capDayMin: 240,
    capWeekMin: 1440,
    multiplierWorking: 1.5,
    multiplierWeeklyOff: 1.5,
    multiplierHoliday: 1.5,
    nightWindowEnabled: false,
    nightWindowStart: '22:00',
    nightWindowEnd: '04:00',
    nightPremiumRate: 0,
  },
  holidayWork: 'substitute_leave',
};

export type AttendanceStatus =
  | 'OffShift' | 'Holiday' | 'WeeklyOff' | 'OnLeave'
  | 'Present' | 'HalfDay' | 'HalfDayLeave' | 'Absent' | 'Incomplete' | 'WFH';

export type DayType = 'working' | 'weekly_off' | 'holiday';

export type Flag =
  | { kind: 'Late'; minutes: number }
  | { kind: 'EarlyOut'; minutes: number }
  | { kind: 'HolidayWork'; workedMin: number }
  | { kind: 'OTCapExceeded'; minutes: number }
  | { kind: 'OTPreapprovalMissing'; rawMinutes: number }
  | { kind: 'MissingOut' }
  | { kind: 'UnpairedPunch'; punchId: string };

export interface OtResult {
  status: 'None' | 'Detected';
  minutes: number;
  rawMinutes: number;
  multiplier: number;
  dayType: DayType;
  nightMinutes: number;
  nightPremiumRate: number;
}

export interface Pair { inMs: number; outMs: number; inId: string; outId: string }

export interface ComputeDayInput {
  employeeId: string;
  localDate: string;
  shift: Shift | null;
  punches: readonly Punch[];
  ctx: DayContext;
  policy: AttendancePolicy;
}

export interface AttendanceDay {
  employeeId: string;
  localDate: string;
  shiftCode: string | null;
  status: AttendanceStatus;
  window: { startMs: number; endMs: number } | null;
  scheduledMin: number;
  firstInMs: number | null;
  lastOutMs: number | null;
  workedMin: number;
  breakMin: number;
  lateMin: number;
  earlyMin: number;
  ot: OtResult;
  flags: Flag[];
  pairs: Pair[];
  /** Ids of punches inside the shift window, in order. Off-window punches never affect the day. */
  punchIds: string[];
  inputsHash: string;
}
