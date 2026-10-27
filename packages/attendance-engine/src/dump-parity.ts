// Writes seeded, varied engine inputs with the TS outputs as JSON so the Python port (server/engines/attendance) can be
// checked against them. Usage: tsx src/dump-parity.ts <output dir>  -> attendance_compute.json, attendance_misc.json
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
import {
  canonicalJson, computeDay, DEFAULT_POLICY, localDateOf, localMidnightMs, localTimeMs, minutesBetween, parseHHMM,
  roundMinutes, sha256Hex, type AttendancePolicy, type ComputeDayInput, type DayContext, type Punch, type Shift,
} from './index.js';

// mulberry32: small, seedable, identical on every run.
let seed = 20260921;
const rnd = (): number => {
  seed = (seed + 0x6d2b79f5) | 0;
  let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
  t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
};
const int = (lo: number, hi: number) => lo + Math.floor(rnd() * (hi - lo + 1));
const pick = <T>(xs: readonly T[]): T => xs[Math.floor(rnd() * xs.length)]!;
const chance = (p: number) => rnd() < p;
const hhmm = (m: number) => `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
const ASCII = Array.from({ length: 128 }, (_, i) => String.fromCharCode(i)).join('');
const ID_CHARS = 'abcABC019-_. xyzXYZ:/~$#\t';
const asciiString = (alphabet: string, maxLen: number) => Array.from({ length: int(0, maxLen) }, () => pick([...alphabet])).join('');
const uuidish = () => Array.from({ length: 32 }, () => '0123456789abcdef'[int(0, 15)]).join('').replace(/^(.{8})(.{4})(.{4})(.{4})/, '$1-$2-$3-$4-');

function randomDate(): string {
  const ms = Date.UTC(2019, 0, 1) + int(0, 12 * 366) * 86_400_000;
  return new Date(ms).toISOString().slice(0, 10);
}

function randomShift(): Shift & { id?: string } {
  const night = chance(0.35);
  const step = pick([1, 5, 15, 30, 60]);
  const start = night ? int(17 * 60, 23 * 60 + 59) : int(0, 1439);
  let end: number;
  const r = rnd();
  if (r < 0.05) end = start; // 24 h shift
  else if (night) end = int(0, 10 * 60);
  else end = (start + int(60, 12 * 60)) % 1440;
  const s: Shift & { id?: string } = {
    code: pick(['GEN', 'NIGHT', 'SPLIT', 'S']),
    start: hhmm(Math.floor(start / step) * step), end: hhmm(Math.floor(end / step) * step),
    graceInMin: pick([0, 5, 10, 15, 30, int(0, 60)]), graceOutMin: pick([0, 0, 5, 15, int(0, 60)]),
    earlyInWindowMin: pick([0, 30, 60, 120, int(0, 240)]), lateOutWindowMin: pick([0, 60, 120, 360, int(0, 600)]),
    breakMinutes: pick([0, 0, 30, 45, 60, int(0, 120)]),
    halfDayThresholdMin: pick([0, 240, 270, int(0, 400)]), absentThresholdMin: pick([0, 120, int(0, 200)]),
    pairingMode: pick(['alternating', 'strict_log_type', 'first_last'] as const),
    roundingMin: pick([0, 0, 5, 10, 15, 30, 7]), roundingDirection: pick(['nearest', 'down', 'up'] as const),
  };
  // The API passes the shift row id along (repo.toShift), so it is part of stored hashes.
  if (chance(0.5)) s.id = uuidish();
  return s;
}

function randomPunches(date: string, shift: Shift | null): Punch[] {
  const n = pick([0, 1, 2, 2, 2, 3, 4, 4, 5, 6, 8]);
  const anchorStart = localTimeMs(date, shift?.start ?? '09:00');
  let anchorEnd = localTimeMs(date, shift?.end ?? '18:00');
  if (anchorEnd <= anchorStart) anchorEnd += 86_400_000;
  const out: Punch[] = [];
  for (let i = 0; i < n; i++) {
    const r = rnd();
    let at: number;
    if (r < 0.35) at = anchorStart + int(-120, 120) * 60_000;
    else if (r < 0.7) at = anchorEnd + int(-180, 420) * 60_000;
    else if (r < 0.9) at = anchorStart + Math.floor(rnd() * (anchorEnd - anchorStart));
    else at = anchorStart + int(-36, 60) * 3_600_000; // often off-window
    const jitter = rnd();
    if (jitter < 0.25) at += int(0, 59) * 1000;
    else if (jitter < 0.35) at += int(0, 59_999);
    else if (jitter < 0.37) at += rnd(); // fractional ms
    else at = Math.floor(at / 60_000) * 60_000;
    out.push({
      id: chance(0.7) ? uuidish() : chance(0.5) ? `p${int(1, 200)}` : asciiString(ID_CHARS, 6),
      at, direction: pick(['in', 'out', 'unknown', 'unknown'] as const),
      verifyMode: pick(['fingerprint', 'face', 'card', 'password']),
      source: pick(['zkteco', 'zkteco', 'mobile', 'web', 'auto_clock_out']),
    });
  }
  // Same-instant punches with ids that sort differently under localeCompare and code-unit order.
  if (out.length > 0 && chance(0.25)) {
    const base = out[int(0, out.length - 1)]!;
    for (let k = int(1, 3); k > 0; k--) out.push({ ...base, id: asciiString(ID_CHARS, 5), direction: pick(['in', 'out', 'unknown'] as const) });
  }
  return out;
}

function randomCtx(shift: Shift | null): DayContext {
  const ctx: DayContext = {
    holiday: chance(0.12), weeklyOff: chance(0.12),
    leave: pick(['none', 'none', 'none', 'full', 'first_half', 'second_half'] as const),
    wfh: chance(0.1), otEligible: chance(0.8),
    otPreapprovedMin: pick([null, null, 0, 30, 60, 120, 240, 300, int(0, 600)]),
    otMinutesThisWeek: pick([0, 0, 0, 600, 1380, 1440, 1500, int(0, 1440)]),
  };
  const r = rnd();
  if (r < 0.05) ctx.shortLeave = [];
  else if (r < 0.35) {
    const base = parseHHMM(shift?.start ?? '10:00');
    ctx.shortLeave = Array.from({ length: int(1, 3) }, () => {
      const from = (base + 1440 + int(-60, 720)) % 1440;
      const to = chance(0.1) ? int(0, 1439) : (from + pick([30, 60, 90, 120, int(1, 300)])) % 1440;
      return { from: hhmm(Math.floor(from / 5) * 5 % 1440), to: hhmm(to) };
    });
  }
  return ctx;
}

function randomPolicy(): AttendancePolicy {
  return {
    version: pick(['default-v1', 'v2', 'company-7', 'नीति-२', 'v"q\\uote\n']),
    holidayWork: pick(['substitute_leave', 'pay', 'employee_choice'] as const),
    ot: {
      minThresholdMin: pick([0, 15, 30, 60]), roundingMin: pick([0, 1, 15, 30, 7]), requirePreapproval: chance(0.6),
      capDayMin: pick([240, 120, 0, int(0, 240)]), capWeekMin: pick([1440, 600, int(0, 1440)]),
      multiplierWorking: pick([1.5, 2, 1.25, 1]), multiplierWeeklyOff: pick([1.5, 2, 1.75]), multiplierHoliday: pick([1.5, 2, 2.5]),
      nightWindowEnabled: chance(0.5),
      nightWindowStart: pick(['22:00', '20:00', '00:00', hhmm(int(0, 1439))]),
      nightWindowEnd: pick(['04:00', '06:00', '23:00', hhmm(int(0, 1439))]),
      nightPremiumRate: pick([0, 0.25, 0.5, 1.1, 0.1 + 0.2, 1e-7, 123456789.123]),
    },
  };
}

// Policies are shared by index ("policy": n into "policies") to keep the fixture small.
const policies = [DEFAULT_POLICY, ...Array.from({ length: 60 }, randomPolicy)];
const compute: Array<{ input: ComputeDayInput; output?: unknown; error?: string }> = [];
for (let i = 0; i < 3000; i++) {
  const date = randomDate();
  const shift = chance(0.04) ? null : randomShift();
  const input: ComputeDayInput = {
    employeeId: chance(0.9) ? uuidish() : 'E', localDate: date, shift,
    punches: randomPunches(date, shift), ctx: randomCtx(shift), policy: chance(0.4) ? DEFAULT_POLICY : pick(policies),
  };
  compute.push({ input, output: computeDay(input) });
}
// Inputs the engine must reject the same way.
const bad: Array<[string, Partial<Shift>, Partial<DayContext>]> = [
  ['2026-02-30', {}, {}], ['2026-13-01', {}, {}], ['0099-01-01', {}, {}], ['2026-9-21', {}, {}],
  ['2026-09-21', { start: '24:00' }, {}], ['2026-09-21', { end: '9:00' }, {}], ['2026-09-21', {}, { shortLeave: [{ from: '10:60', to: '11:00' }] }],
];
for (const [localDate, sOver, cOver] of bad) {
  const input: ComputeDayInput = {
    employeeId: 'E', localDate, shift: { ...randomShift(), ...sOver }, punches: [], ctx: { ...randomCtx(null), leave: 'none', holiday: false, weeklyOff: false, ...cOver }, policy: DEFAULT_POLICY,
  };
  try {
    compute.push({ input, output: computeDay(input) });
  } catch (e) {
    compute.push({ input, error: (e as Error).name });
  }
}

// Hashing of arbitrary JSON values, including keys and strings outside ASCII.
const STR_CHARS = ['a', 'Z', '"', '\\', '\n', '\t', '\u0001', '\u001f', '\u007f', 'é', 'न', ' ', '😀', '\ud800', '\udfff', 'ﬀ', '_'];
const randStr = () => Array.from({ length: int(0, 6) }, () => pick(STR_CHARS)).join('');
const randNum = () => pick([0, 1, -1, 1.5, 2 ** 53, 2 ** 60, 1e21, 1e-7, 123e-20, 0.1 + 0.2, -1e300, 5e-324, int(-1e9, 1e9), rnd() * 1e6, rnd()]);
function randValue(depth: number): unknown {
  const r = rnd();
  if (depth > 3 || r < 0.3) return pick([null, true, false, randNum(), randStr()]);
  if (r < 0.6) return Array.from({ length: int(0, 4) }, () => randValue(depth + 1));
  const o: Record<string, unknown> = {};
  for (let k = int(0, 5); k > 0; k--) o[randStr()] = randValue(depth + 1);
  return o;
}
const hashes = Array.from({ length: 500 }, () => {
  const value = randValue(0);
  const json = canonicalJson(value);
  return { value, json, sha256: sha256Hex(json) };
});

const collation = Array.from({ length: 3000 }, () => {
  const alphabet = chance(0.5) ? ASCII : ID_CHARS;
  const a = asciiString(alphabet, 6);
  const b = chance(0.3) ? a.toUpperCase() : asciiString(alphabet, 6);
  return { a, b, cmp: Math.sign(a.localeCompare(b)) };
});

const time = {
  localDateOf: Array.from({ length: 500 }, () => {
    const ms = Date.UTC(2026, 0, 1) + Math.floor((rnd() - 0.5) * 4e11) + (chance(0.1) ? rnd() : 0);
    return { ms, date: localDateOf(ms) };
  }),
  localMidnightMs: ['2026-09-21', '2000-02-29', '2100-02-29', '1970-01-01', '0100-03-01', '9999-12-31', '2026-04-31', '0050-01-01', '2026-1-01', '٢٠٢٦-09-21']
    .map((d) => { try { return { d, ms: localMidnightMs(d) }; } catch (e) { return { d, error: (e as Error).name }; } }),
  parseHHMM: ['00:00', '23:59', '24:00', '12:60', '9:00', '09:00 ', '٠٩:00', '09:00\n']
    .map((s) => { try { return { s, min: parseHHMM(s) }; } catch (e) { return { s, error: (e as Error).name }; } }),
  roundMinutes: Array.from({ length: 1000 }, () => {
    const min = pick([int(-100, 1000), int(-100, 1000) + 0.5, rnd() * 1000 - 100, 7.5, -7.5, 2.5, -2.5, 0.49999999999999994]);
    const step = pick([0, 1, 2, 5, 15, 30, 7, -5, 0.5]);
    const direction = pick(['nearest', 'down', 'up'] as const);
    return { min, step, direction, out: roundMinutes(min, step, direction) };
  }),
  minutesBetween: Array.from({ length: 300 }, () => {
    const a = Date.UTC(2026, 0, 1) + int(-1e9, 1e9) + (chance(0.2) ? rnd() : 0);
    const b = a + int(-1e7, 1e7) + (chance(0.2) ? rnd() : 0);
    return { a, b, out: minutesBetween(a, b) };
  }),
};

const dir = process.argv[2]!;
const cases = compute.map(({ input, ...rest }) => ({ input: { ...input, policy: policies.indexOf(input.policy) }, ...rest }));
writeFileSync(join(dir, 'attendance_compute.json'), JSON.stringify({ policies, cases }));
writeFileSync(join(dir, 'attendance_misc.json'), JSON.stringify({ hashes, collation, time }));
