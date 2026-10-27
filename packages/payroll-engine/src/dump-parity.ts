// Writes TS engine outputs for a wide, seeded set of inputs as JSON, so the Python port (server/engines/payroll) can be
// checked against them field for field:
//   apps/api/node_modules/.bin/tsx packages/payroll-engine/src/dump-parity.ts server/tests/parity/fixtures
import { readdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { computePayslip, payableFraction } from './compute.js';
import { D, Decimal, paisa, rupee } from './decimal.js';
import { DEFAULT_COMPONENTS, DEFAULT_RULES, TAX_SCHEDULE_2082, TAX_SCHEDULE_2083, rulesForFy } from './defaults.js';
import { baseInput } from './fixtures.js';
import { evaluateFormula, formulaReferences, parseFormula, validateFormula, type EvalEnv } from './formula.js';
import { optimumCit } from './optimum.js';
import { computeTds, type TaxInput } from './tax.js';
import type { ComponentDef, PayrollInput, ProrationBasis } from './types.js';
import { amountInWordsEn, amountInWordsNe, formatNpr } from './words.js';

const outDir = process.argv[2];
if (!outDir) throw new Error('usage: dump-parity.ts <output dir>');
const here = path.dirname(fileURLToPath(import.meta.url));

/** mulberry32: a small seeded PRNG so the dump is reproducible. */
function rng(seed: number) {
  let a = seed >>> 0;
  const next = () => { a = (a + 0x6d2b79f5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  const int = (lo: number, hi: number) => lo + Math.floor(next() * (hi - lo + 1));
  const pick = <T>(xs: readonly T[]): T => xs[int(0, xs.length - 1)]!;
  const chance = (p: number) => next() < p;
  const money = (max: number) => (int(0, max * 100) / 100).toFixed(2);
  return { next, int, pick, chance, money };
}

const err = (e: unknown) => ({ error: (e as Error).constructor.name, message: (e as Error).message });
const value = (v: unknown) => v instanceof Decimal ? { d: v.toString() } : typeof v === 'boolean' ? { b: v } : typeof v === 'string' ? { s: v } : { other: typeof v };
const attempt = <T>(f: () => T): { ok: T } | { error: string; message: string } => { try { return { ok: f() }; } catch (e) { return err(e); } };
const write = (name: string, data: unknown) => writeFileSync(path.join(outDir, `payroll_${name}.json`), JSON.stringify(data) + '\n');

/** Serialises an input compactly: the default component set and default rule sets are referenced, not repeated. */
function packInput(input: PayrollInput): unknown {
  const rulesJson = JSON.stringify(input.rules);
  const fy = [2082, 2083].find((y) => JSON.stringify(rulesForFy(y)) === rulesJson);
  return { ...input, components: input.components === DEFAULT_COMPONENTS ? 'DEFAULT' : input.components, rules: fy ? { $rulesForFy: fy } : input.rules };
}

// ---------------------------------------------------------------------------------------------------------------------
// Defaults: the Python constants must equal the TS ones.
write('defaults', { DEFAULT_COMPONENTS, DEFAULT_RULES, TAX_SCHEDULE_2082, TAX_SCHEDULE_2083 });

// ---------------------------------------------------------------------------------------------------------------------
// Payslips: the golden inputs, a seeded random sweep, custom formula components, rule() lookups and error cases.
const payslips: Array<{ name: string; input: unknown; result: unknown }> = [];
const addPayslip = (name: string, input: PayrollInput) => payslips.push({ name, input: packInput(input), result: attempt(() => computePayslip(input)) });

for (const f of readdirSync(path.join(here, 'golden')).filter((x) => x.endsWith('.json')).sort()) {
  addPayslip(`golden/${f}`, (JSON.parse(readFileSync(path.join(here, 'golden', f), 'utf8')) as { input: PayrollInput }).input);
}

const r = rng(20830401);
const e0 = baseInput().employee;
const d0 = baseInput().declaration;
for (let i = 0; i < 400; i++) {
  const fy = r.pick([2082, 2083]);
  const monthIndex = r.int(1, 12);
  const days = r.pick([29, 30, 31, 32]);
  const paid = r.chance(0.6) ? days : r.chance(0.5) ? r.int(0, days) : r.int(0, days * 2) / 2;
  const basis = r.pick<ProrationBasis>(['bs_calendar_days', 'bs_calendar_days', 'fixed_30', '26_day']);
  const base: Record<string, string> = { BASIC: String(r.pick([0, 9000, 12170, 30000, 40000, 85000, 150000, 400000, r.int(10000, 600000)])) };
  if (r.chance(0.7)) base.ALLOWANCE = r.money(200000);
  if (r.chance(0.3)) base.DA = r.pick(['7380', '7550', r.money(20000)]);
  if (r.chance(0.3)) base.HRA = r.money(50000);
  if (r.chance(0.05)) base.ABSENCE = r.money(5000);
  const inputs: PayrollInput['inputs'] = [];
  for (let k = r.int(0, 3); k > 0; k--) inputs.push({ code: r.pick(['BONUS', 'ARREARS', 'ABSENCE', 'ADVANCE', 'BASIC', 'ALLOWANCE', 'NOTICE_PAY', 'INSURANCE_LIFE']), amount: r.money(r.pick([1000, 50000, 200000])) });
  const loans: PayrollInput['loans'] = [];
  for (let k = r.int(0, 2); k > 0; k--) loans.push({ id: `L${k}`, emi: r.money(30000), remaining: r.money(100000) });
  const extras: NonNullable<PayrollInput['extras']> = {};
  if (r.chance(0.2)) extras.monthsOfServiceInFy = r.int(1, 12);
  if (r.chance(0.08)) extras.forceFestivalAllowance = true;
  if (r.chance(0.1)) { extras.isFinalSettlement = true; extras.leaveEncashDays = String(r.int(0, 60)); if (r.chance(0.5)) extras.leaveEncashDivisor = r.pick([26, 30]); if (r.chance(0.3)) extras.lastBasic = r.money(200000); if (r.chance(0.5)) extras.recoverLoansInFull = true; }
  if (r.chance(0.05)) extras.deductFullTaxNow = true;
  if (r.chance(0.05)) extras.recurringOverride = { taxable: r.money(200000), retirement: r.money(30000) };
  const input = baseInput({
    fy, period: { bsYear: fy, bsMonth: ((monthIndex + 2) % 12) + 1, daysInMonth: days, fiscalYearStart: fy, monthIndexInFy: monthIndex },
    base, inputs, loans,
    attendance: { paidDays: String(paid), lopDays: String(days - paid), otHours: String(r.chance(0.3) ? r.int(1, 40) : 0), ...(r.chance(0.2) ? { otDayType: r.pick(['working', 'weekly_off', 'holiday'] as const) } : {}) },
    employee: { ...e0, regime: r.pick(['ssf', 'pf', 'none'] as const), gender: r.pick(['male', 'female', 'other', null] as const), maritalStatus: r.pick(['single', 'couple', null] as const),
      isResident: r.chance(0.9), remoteAreaClass: r.chance(0.2) ? r.pick(['A', 'B', 'C', 'D'] as const) : null, disability: r.chance(0.1), otEligible: r.chance(0.8),
      festivalMonth: r.chance(0.2) ? r.int(1, 12) : null, ...(r.chance(0.1) ? { remunerationOnlyIncome: false } : {}) },
    ytd: { taxable: r.chance(0.5) ? '0' : r.money(2000000), tdsPaid: r.chance(0.5) ? '0' : r.money(200000), retirementEmployee: r.money(100000), retirementEmployer: r.money(150000), festivalPaid: r.chance(0.4) },
    declaration: { ...d0, lifePremium: r.chance(0.5) ? '0' : r.money(80000), healthPremium: r.chance(0.7) ? '0' : r.money(40000), citMonthly: r.chance(0.7) ? '0' : r.money(30000),
      medicalCost: r.chance(0.7) ? '0' : r.money(30000), otherRetirementAnnual: r.chance(0.8) ? '0' : r.money(100000), ...(r.chance(0.2) ? { donation: r.money(300000) } : {}) },
    policy: { prorationBasis: basis, hourlyDivisor: r.pick([208, 208, 240]), rounding: r.pick(['half_up_rupee', 'half_up_rupee', 'paisa'] as const), includeEmployerContributionInIncome: r.chance(0.8), fullTime: r.chance(0.9) },
    ...(Object.keys(extras).length ? { extras } : {}),
  });
  addPayslip(`random/${i}`, input);
}

const comp = (code: string, over: Partial<ComponentDef> = {}): ComponentDef => ({ code, type: 'earning', nameEn: code, dependsOnPaymentDays: false, taxable: true, rounding: 'paisa', sortOrder: 1, ...over });
const customCases: Array<[string, ComponentDef[], Partial<Parameters<typeof baseInput>[0]>?]> = [
  ['formula_graph', [...DEFAULT_COMPONENTS.filter((c) => c.code !== 'HRA'), comp('HRA', { formula: 'BASIC * 0.2', condition: 'BASIC > 10000', sortOrder: 30, dependsOnPaymentDays: true }), comp('TRANSPORT', { formula: 'HRA / 2', sortOrder: 5 })], { attendance: { paidDays: '20', lopDays: '11', otHours: '0' } }],
  ['condition_false', [...DEFAULT_COMPONENTS, comp('X', { formula: 'BASIC * 0.1', condition: 'regime == "pf"' })]],
  ['condition_not_bool', [...DEFAULT_COMPONENTS, comp('X', { formula: '1', condition: 'BASIC' })]],
  ['formula_string', [...DEFAULT_COMPONENTS, comp('X', { formula: 'regime' })]],
  ['formula_bool', [...DEFAULT_COMPONENTS, comp('X', { formula: 'is_female' })]],
  ['cycle', [...DEFAULT_COMPONENTS, comp('X', { formula: 'Y + 1' }), comp('Y', { formula: 'Z + 1', sortOrder: 2 }), comp('Z', { formula: 'X', sortOrder: 3 })]],
  ['self_reference', [...DEFAULT_COMPONENTS, comp('X', { formula: 'X + 1' })]],
  ['too_large', [...DEFAULT_COMPONENTS, comp('X', { formula: '1e12' })]],
  ['just_below_limit', [...DEFAULT_COMPONENTS, comp('X', { formula: '999999999999.99' })], { base: {} }],
  ['negative_earning', [...DEFAULT_COMPONENTS, comp('X', { formula: '0 - 500' })]],
  ['negative_zero_earning', [...DEFAULT_COMPONENTS, comp('X', { formula: '-0' })]],
  ['negative_statistical', [...DEFAULT_COMPONENTS, comp('STAT', { type: 'statistical', formula: '-1234.565', sortOrder: 500 }), comp('USE', { type: 'deduction', formula: 'abs(STAT) + 0.005', sortOrder: 266 })]],
  ['negative_zero_statistical', [...DEFAULT_COMPONENTS, comp('STAT', { type: 'statistical', formula: '0 * -1', sortOrder: 500 })]],
  ['deduction_formula_capped', [...DEFAULT_COMPONENTS, comp('UNION', { type: 'deduction', formula: 'GROSS * 2', sortOrder: 265 })]],
  ['rupee_rounding_formula', [...DEFAULT_COMPONENTS, comp('X', { formula: 'BASIC / 7', rounding: 'rupee' }), comp('Y', { formula: 'BASIC / 7', statutory: true, type: 'deduction', sortOrder: 205 })]],
  ['paisa_policy', [...DEFAULT_COMPONENTS, comp('X', { formula: 'BASIC / 7' })], { policy: { ...baseInput().policy, rounding: 'paisa' } }],
  ['sort_collisions', [...DEFAULT_COMPONENTS, ...['a_B', 'AB', 'ab', 'A_b', 'A-1', 'A1', 'a10', 'a9', 'Ab', 'aB', 'B', '_x', 'Z9', 'z_'].map((code, i) => comp(code, { formula: String(100 + i), sortOrder: 45 }))]],
  ['proto_toString_code', [...DEFAULT_COMPONENTS, comp('toString', { sortOrder: 41 })]],
  ['proto_constructor_code', [...DEFAULT_COMPONENTS, comp('constructor', { formula: '5', sortOrder: 42 }), comp('USE_CTOR', { formula: 'constructor * 2', sortOrder: 43 })]],
  ['proto_code', [...DEFAULT_COMPONENTS, comp('__proto__', { formula: '5', sortOrder: 42 }), comp('USE', { formula: '__proto__ * 2', sortOrder: 43 })]],
  ['valueOf_base', [...DEFAULT_COMPONENTS, comp('valueOf', { sortOrder: 41 })], { base: { BASIC: '40000', valueOf: '100' } }],
  ['builtin_vars', [...DEFAULT_COMPONENTS, comp('X', { type: 'statistical', formula: 'days_in_bs_month + paid_days * 1000 + lop_days * 100000 + hourly_divisor / 1000 + ot_hours + months_in_service * 10000000 + bs_month + fiscal_year / 10000', sortOrder: 500 })], { attendance: { paidDays: '29.5', lopDays: '1.5', otHours: '3' }, extras: { monthsOfServiceInFy: 7 } }],
  ['formula_on_builtin_code', [...DEFAULT_COMPONENTS.filter((c) => c.code !== 'OT'), comp('OT', { formula: 'ot_hours * 500', sortOrder: 50 })], { attendance: { paidDays: '31', lopDays: '0', otHours: '7' } }],
  ['duplicate_codes', [...DEFAULT_COMPONENTS, comp('X', { formula: '100', sortOrder: 2 }), comp('X', { formula: '200', sortOrder: 1 })]],
  ['earning_named_tds', [...DEFAULT_COMPONENTS.filter((c) => c.code !== 'TDS'), comp('TDS', { sortOrder: 15 })]],
  ['no_tds_component', DEFAULT_COMPONENTS.filter((c) => c.code !== 'TDS')],
  ['no_gross_with_loans', DEFAULT_COMPONENTS.filter((c) => c.code !== 'GROSS'), { loans: [{ id: 'L', emi: '5000', remaining: '9000' }] }],
  ['hourly_divisor_zero', DEFAULT_COMPONENTS, { attendance: { paidDays: '31', lopDays: '0', otHours: '5' }, policy: { ...baseInput().policy, hourlyDivisor: 0 } }],
  ['days_in_month_zero', DEFAULT_COMPONENTS, { period: { bsYear: 2083, bsMonth: 4, daysInMonth: 0, fiscalYearStart: 2083, monthIndexInFy: 1 }, attendance: { paidDays: '0', lopDays: '0', otHours: '0' } }],
  ['bad_ot_day_type', DEFAULT_COMPONENTS, { attendance: { paidDays: '31', lopDays: '0', otHours: '5', otDayType: 'sunday' as 'working' } }],
  ['bad_amount_string', DEFAULT_COMPONENTS, { base: { BASIC: '40,000' } }],
  ['whitespace_amount', DEFAULT_COMPONENTS, { base: { BASIC: ' 40000' } }],
  ['underscore_amount', DEFAULT_COMPONENTS, { base: { BASIC: '40_000' } }],
  ['hex_amount', DEFAULT_COMPONENTS, { base: { BASIC: '0x9C40' } }],
  ['exp_amount', DEFAULT_COMPONENTS, { base: { BASIC: '4e4', ALLOWANCE: '+2.0E4' } }],
  ['festival_month_negative', DEFAULT_COMPONENTS, { employee: { ...e0, festivalMonth: -20 } }],
  ['last_basic_null', DEFAULT_COMPONENTS, { extras: { leaveEncashDays: '10', lastBasic: null as unknown as string } }],
  ['empty_extras', DEFAULT_COMPONENTS, { extras: {} }],
  ['empty_formula_and_condition', [...DEFAULT_COMPONENTS, comp('X', { formula: '', condition: '' })], { base: { BASIC: '40000', X: '321' } }],
  ['formula_parse_error', [...DEFAULT_COMPONENTS, comp('X', { formula: 'BASIC +' })]],
  ['condition_parse_error', [...DEFAULT_COMPONENTS, comp('X', { formula: '1', condition: '(' })]],
];
for (const [name, components, over] of customCases) addPayslip(`custom/${name}`, baseInput({ components, ...(over ?? {}) }));

// rule() lookups through the engine's own resolver (compute.ts ruleLookup), with odd rule values added.
const oddRules = { ...rulesForFy(2083), label: 'bs_calendar_days', num: 5, flt: 0.1, big: 1e21, tiny: 1e-7, bool: true, nul: null, hex: '0x1A', bin: '0b101', oct: '0o17', hexfrac: '0x1.8', hexp: '0x1p4',
  inf: 'Infinity', ninf: '-Infinity', nan: 'NaN', under: '1_000', neg: '-5.5e3', ws: ' 5', empty: '', arr: [1, '2', [3]], nested: { a: { b: '7', n: null } }, emoji: '😀x' } as unknown as PayrollInput['rules'];
const ruleExprs = [
  'rule("ssf.rates", "employee") * 100', 'rule("tax.schedule", "bands.all.0.rate")', 'rule("tax.schedule", "bands.all.length")', 'rule("tax.schedule", "bands.all.4.upTo")',
  'rule("tax.schedule", "bands.all.9.rate")', 'rule("tax.schedule", "fiscalYearStart")', 'rule("min_wage")', 'rule("min_wage", "")', 'rule("min_wage", "total")', 'rule("nope")', 'rule("min_wage", "nope")',
  'rule("num")', 'rule("flt") * 10', 'rule("big")', 'rule("tiny") * 1e7', 'if(rule("bool"), 1, 2)', 'rule("nul")', 'rule("hex")', 'rule("bin")', 'rule("oct")', 'rule("hexfrac")', 'rule("hexp")',
  'rule("inf")', 'rule("ninf")', 'if(rule("nan") == "NaN", 1, 0)', 'rule("under")', 'rule("neg")', 'if(rule("ws") == " 5", 1, 0)', 'if(rule("empty") == "", 1, 0)',
  'rule("arr")', 'rule("arr", "0")', 'rule("arr", "1")', 'rule("arr", "length")', 'rule("arr", "2.0")', 'rule("arr", "01")', 'rule("nested", "a.b")', 'rule("nested", "a.n")', 'rule("nested", "a.b.length")',
  'rule("label", "length")', 'if(rule("label", "0") == "b", 1, 0)', 'rule("emoji", "length")', 'rule("constructor")', 'rule("toString")', 'rule("__proto__")', 'rule("hasOwnProperty")',
  'rule("min_wage", "toString")', 'rule("min_wage", "__proto__")', 'rule("num", "toFixed")', 'rule("num", "x")', 'rule("ssf.rates", "allocation.old_age") * 10000', 'rule(1)', 'rule("x", 2)', 'rule()',
];
for (const [i, expr] of ruleExprs.entries()) addPayslip(`rule/${i}`, baseInput({ components: [...DEFAULT_COMPONENTS, comp('X', { type: 'statistical', formula: expr, sortOrder: 500 })], rules: oddRules }));

// ---------------------------------------------------------------------------------------------------------------------
// Formula DSL: parse tree, value, references and validation for hand-written expressions, token soup and random text.
const env: EvalEnv = {
  vars: { BASIC: D('40000'), DA: D('7380'), paid_days: D(22), days_in_bs_month: D(31), is_female: true, regime: 'ssf', NEG: D('-0'), BIG: D('1e25'), SMALL: D('1e-20'), label: 'abc', num: 5 as unknown as Decimal, nul: null as unknown as Decimal },
  rule: (key, p) => (key === 'ssf.rates' && p === 'employee' ? D('0.11') : key === 'min_wage' && p === 'total' ? D('19550') : key === 'text' ? 'text' : key === 'flag' ? true : key === 'raw' ? (7 as unknown as Decimal) : D(0)),
};
const handwritten = [
  'BASIC * 0.11', '1 + 2 * 3 - 4 / 8', '-(BASIC + DA)', 'BASIC * paid_days / days_in_bs_month', 'min(BASIC, 12170, 99999)', 'max(BASIC + DA, rule("min_wage", "total"))',
  'round(BASIC * paid_days / days_in_bs_month, 2)', 'round(2.5)', 'round(-2.5)', 'round(2.45, 1)', 'round(-0.004, 2)', 'round(1.23456789012, 10)', 'round(5, 0)', 'round(1.5, 2.5)', 'round(1.5, 11)', 'round(1.5, -1)',
  'floor(7.9) + ceil(7.1)', 'floor(-7.1)', 'ceil(-0.5)', 'abs(-3)', 'abs(NEG)', 'if(is_female and regime == "ssf", 1, 0)', 'if(not is_female or BASIC < 1000, 1, 0)', 'BASIC * rule("ssf.rates", "employee")',
  'BASIC + HRA', 'eval("x")', 'round()', 'BASIC / 0', 'BASIC + is_female', 'if(BASIC, 1, 2)', 'BASIC +', 'BASIC.toString()', 'BASIC; DROP TABLE', '1 / 3', '2 / 3', '10 / 7 * 7', '1e400', '-1e400',
  '999999999999999999999 + 1', '123456789012345678901234567890 * 9', '0.1 + 0.2', '1 - 0.9', '1e-7', '1e21', '1e20', '0.000001', '5.', '.5', '..5', '1.2.3', '1e', '1e+', '1E5', '00012', '1_000',
  'regime == "ssf"', 'regime != "pf"', "regime == 'ssf'", 'regime == ssf', '"a" == "a"', '"a" < "b"', 'true == 1', 'true == true', 'is_female == true', '1 == 1.0', 'BASIC == 40000.000', 'NEG == 0', 'label == "abc"',
  'num', 'nul', 'NEG', 'NEG + 0', '0 + NEG', 'NEG - 0', '0 - NEG', 'NEG * -1', '-NEG', '0 * -1', '-0', '-0 + 0', '-0 - 0', 'min(0, NEG)', 'min(NEG, 0)', 'max(0, NEG)', 'max(NEG, 0)', 'min(1)', 'max(3, 1, 2)',
  'BIG * BIG', 'BIG * BIG * BIG * BIG', 'SMALL * SMALL', 'SMALL / BIG', 'BIG / SMALL', '1e9000000000000000 * 10', '1e9000000000000001', '1e9000000000000001 - 1e9000000000000001', '1e-9000000000000000 / 10', '1e-9000000000000001',
  'rule("text")', 'rule("text") + 1', 'rule("flag")', 'rule("raw")', 'rule("raw") + 1', 'rule(1)', 'rule("a", 1)', 'rule("a", "b", "c")', 'rule()', 'if(1 < 2, "yes", "no")', 'if(true, 1)', 'min()', 'max(' + Array(33).fill('1').join(',') + ')',
  'constructor', '__proto__', 'toString', 'hasOwnProperty', 'valueOf', 'constructor()', 'toString(1)', 'toString(true)', '__proto__(1, 2)', 'hasOwnProperty("x")', 'and', 'or', 'not', 'not not true', 'not 1',
  'true and false', 'true or 1', 'false or 1', 'false and 1', '1 and true', 'true and 1', '1 < 2 < 3', '(1 < 2) == true', '1 <= 1', '2 >= 3', '1 > 0', '1 != 1', '1 = 1', '1 ! 1', '1 & 2', '1 | 2', '$x', 'x$',
  '(', ')', '()', '(1', '1)', '((1))', ',', '1,2', 'f(1,)', 'min(1,,2)', '"unterminated', "'single'", '"a" + 1', '"emoji 😀" == "emoji 😀"', '"😀"', 'é', 'BASIC é', '😀', 'x 😀', ' BASIC', '﻿1', ' 1',
  '\u00851', '\u001c1', '١٢', 'BASIC\t*\n2', '', ' ', 'BASIC * paid_days / days_in_bs_month * days_in_bs_month / paid_days', 'if(paid_days > 20, BASIC, BASIC / 2) + if(regime == "pf", 100, 200)',
  '(' .repeat(64) + '1' + ')'.repeat(64), '('.repeat(65) + '1' + ')'.repeat(65), '-'.repeat(64) + '1', '-'.repeat(65) + '1', 'not '.repeat(64) + 'true', 'not '.repeat(65) + 'true', 'min('.repeat(64) + '1' + ')'.repeat(64),
  '1 + '.repeat(499) + '1', '1 * '.repeat(499) + '1', 'true and '.repeat(300) + 'true', '1 - '.repeat(400) + '1', '2 / '.repeat(300) + '2', '1 < '.repeat(200) + '1', 'x'.repeat(2000), 'x'.repeat(2001), '😀'.repeat(1000), '😀'.repeat(1001),
];
const formulaCases: string[] = [...handwritten];
const tokens = ['BASIC', 'DA', 'paid_days', 'regime', 'is_female', 'NEG', 'BIG', 'SMALL', 'label', 'num', '(', ')', ',', '+', '-', '*', '/', '<', '<=', '>', '>=', '==', '!=', 'and', 'or', 'not', 'if', 'min', 'max', 'round', 'floor',
  'ceil', 'abs', 'rule', '"x"', '"ssf"', '"text"', '"flag"', '"raw"', '0', '1', '2', '2.5', '0.005', '1e400', '-1e400', '999999999999999999999', '1e-30', 'constructor', '__proto__', 'toString', 'true', 'false'];
const fr = rng(42);
for (let i = 0; i < 2500; i++) { const n = fr.int(1, 14); const parts: string[] = []; for (let k = 0; k < n; k++) parts.push(fr.pick(tokens)); formulaCases.push(parts.join(fr.chance(0.8) ? ' ' : '')); }
const gen = (depth: number): string => {
  if (depth <= 0 || fr.chance(0.3)) return fr.pick(['BASIC', 'DA', 'paid_days', 'days_in_bs_month', 'NEG', 'BIG', 'SMALL', String(fr.int(0, 1000)), fr.money(10000), '0.005', '1e-3', '7', '3', '0.1', '2.5']);
  switch (fr.int(0, 6)) {
    case 0: return `${gen(depth - 1)} ${fr.pick(['+', '-', '*', '/'])} ${gen(depth - 1)}`;
    case 1: return `(${gen(depth - 1)})`;
    case 2: return `-${gen(depth - 1)}`;
    case 3: return `${fr.pick(['min', 'max'])}(${gen(depth - 1)}, ${gen(depth - 1)}${fr.chance(0.3) ? ', ' + gen(depth - 1) : ''})`;
    case 4: return `round(${gen(depth - 1)}${fr.chance(0.6) ? ', ' + fr.int(0, 10) : ''})`;
    case 5: return `${fr.pick(['floor', 'ceil', 'abs'])}(${gen(depth - 1)})`;
    default: return `if(${gen(depth - 1)} ${fr.pick(['<', '<=', '>', '>=', '==', '!='])} ${gen(depth - 1)}, ${gen(depth - 1)}, ${gen(depth - 1)})`;
  }
};
for (let i = 0; i < 2000; i++) formulaCases.push(gen(fr.int(1, 6)));
const alphabet = 'BASICDApaid_ys0123456789.eE+-*/<>=!(),"\' \t\nandornotifminmaxround😀é ';
for (let i = 0; i < 800; i++) {
  let s = ''; const n = fr.int(0, 30);
  for (let k = 0; k < n; k++) s += fr.chance(0.05) ? String.fromCharCode(fr.int(0, 0xffff)) : [...alphabet][fr.int(0, [...alphabet].length - 1)];
  formulaCases.push(s);
}
write('formula', formulaCases.map((src) => ({
  src,
  ast: attempt(() => parseFormula(src)),
  value: attempt(() => value(evaluateFormula(src, env))),
  refs: attempt(() => [...formulaReferences(src)]),
  validate: validateFormula(src, ['BASIC', 'DA', 'NEG']),
})));

// Payslips with generated formula components (earning, deduction, employer and statistical), some with conditions.
const pr = rng(5);
const knownOnly = (f: string) => f.replace(/\b(NEG|BIG|SMALL)\b/g, 'ALLOWANCE');
for (let i = 0; i < 150; i++) {
  const extra: ComponentDef[] = [];
  for (let k = pr.int(1, 3); k > 0; k--) {
    extra.push(comp(`F${k}`, {
      type: pr.pick(['earning', 'earning', 'deduction', 'statistical', 'employer_contribution'] as const), formula: knownOnly(gen(pr.int(1, 4))),
      ...(pr.chance(0.3) ? { condition: `${knownOnly(gen(1))} ${pr.pick(['<', '>', '>='])} ${pr.int(0, 50000)}` } : {}),
      dependsOnPaymentDays: pr.chance(0.4), taxable: pr.chance(0.7), rounding: pr.pick(['paisa', 'rupee'] as const), ...(pr.chance(0.2) ? { statutory: true } : {}), sortOrder: pr.int(1, 500),
    }));
  }
  const days = pr.pick([29, 30, 31, 32]);
  const paid = pr.chance(0.6) ? days : pr.int(0, days);
  addPayslip(`generated/${i}`, baseInput({ components: [...DEFAULT_COMPONENTS, ...extra], base: { BASIC: String(pr.int(10000, 300000)), ALLOWANCE: pr.money(50000) },
    period: { bsYear: 2083, bsMonth: 5, daysInMonth: days, fiscalYearStart: 2083, monthIndexInFy: 2 }, attendance: { paidDays: String(paid), lopDays: String(days - paid), otHours: '0' },
    employee: { ...e0, regime: pr.pick(['ssf', 'pf', 'none'] as const) }, policy: { ...baseInput().policy, rounding: pr.pick(['half_up_rupee', 'paisa'] as const) } }));
}
write('payslips', payslips);

// ---------------------------------------------------------------------------------------------------------------------
// Tax: computeTds over an income sweep crossed with status, regime, deductions and timing; plus payableFraction.
const taxCases: Array<{ input: Record<string, unknown>; result: unknown }> = [];
const tr = rng(7);
const edges = ['0', '1', '499999.99', '500000', '500000.01', '600000', '700000', '1000000', '1000000.01', '1100000', '1500000', '2000000', '2500000', '4000000', '4000000.01', '5000000', '5000000.01', '12000000'];
for (let i = 0; i < 1500; i++) {
  const schedule = tr.pick([TAX_SCHEDULE_2082, TAX_SCHEDULE_2083]);
  const income = i < edges.length * 4 ? D(edges[i % edges.length]!).div(12).toFixed(4) : tr.money(tr.pick([50000, 200000, 1000000]));
  const regime = tr.pick(['ssf', 'pf', 'none'] as const);
  const ret = regime === 'none' && tr.chance(0.6) ? '0' : tr.money(20000);
  const input = {
    schedule: schedule === TAX_SCHEDULE_2082 ? 2082 : 2083, regime, isResident: tr.chance(0.9), gender: tr.pick(['male', 'female', 'other', null] as const), maritalStatus: tr.pick(['single', 'couple', null] as const),
    remunerationOnlyIncome: tr.chance(0.85), remoteAreaClass: tr.chance(0.2) ? tr.pick(['A', 'B', 'C', 'D'] as const) : null, disability: tr.chance(0.1),
    monthTaxable: income, recurringTaxable: tr.chance(0.8) ? income : tr.money(100000), futureOneOffs: tr.chance(0.7) ? '0' : tr.money(100000),
    ytdTaxable: tr.chance(0.5) ? '0' : tr.money(3000000), ytdTdsPaid: tr.chance(0.5) ? '0' : tr.money(300000), monthRetirement: ret, recurringRetirement: ret, ytdRetirement: tr.chance(0.5) ? '0' : tr.money(200000),
    otherRetirementAnnual: tr.chance(0.8) ? '0' : tr.money(200000), lifePremium: tr.chance(0.5) ? '0' : tr.money(100000), healthPremium: tr.chance(0.6) ? '0' : tr.money(50000), medicalCost: tr.chance(0.6) ? '0' : tr.money(40000),
    ...(tr.chance(0.3) ? { donation: tr.money(300000) } : {}), monthIndexInFy: tr.int(1, 12), deductFullTaxNow: tr.chance(0.05), isFinalSettlement: tr.chance(0.05),
  };
  const { donation, ...rest } = input;
  const ti: TaxInput = {
    ...rest, schedule, deductions: DEFAULT_RULES['tax.deductions'],
    monthTaxable: D(input.monthTaxable), recurringTaxable: D(input.recurringTaxable), futureOneOffs: D(input.futureOneOffs), ytdTaxable: D(input.ytdTaxable), ytdTdsPaid: D(input.ytdTdsPaid),
    monthRetirement: D(input.monthRetirement), recurringRetirement: D(input.recurringRetirement), ytdRetirement: D(input.ytdRetirement), otherRetirementAnnual: D(input.otherRetirementAnnual),
    lifePremium: D(input.lifePremium), healthPremium: D(input.healthPremium), medicalCost: D(input.medicalCost), ...(donation !== undefined ? { donation: D(donation) } : {}),
  };
  taxCases.push({ input, result: attempt(() => computeTds(ti)) });
}
const fractions: Array<{ paid: string; days: number; basis: ProrationBasis; result: unknown }> = [];
for (const days of [0, 26, 29, 30, 31, 32]) for (const basis of ['bs_calendar_days', 'fixed_30', '26_day'] as const) for (const paid of ['0', '0.5', '1', '10', '22', '25.5', '26', '29', '30', '31', '32', '40', '-3']) {
  fractions.push({ paid, days, basis, result: attempt(() => payableFraction(D(paid), days, basis).toFixed(10)) });
}
write('tax', { tds: taxCases, fractions });

// ---------------------------------------------------------------------------------------------------------------------
// Words, NPR formatting and the money helpers.
const amounts = ['0', '0.004', '0.005', '-0.004', '-0.005', '-0', '0.01', '0.99', '1', '7', '10', '11', '19', '20', '21', '99', '100', '101', '110', '999', '1000', '1001', '1500.50', '-1500.50', '2163.46', '9999.995',
  '10000', '99999', '100000', '100001', '999999', '1000000', '1500000', '9999999', '10000000', '10000001', '12345678.5', '99999999.99', '100000000', '1234567890.12', '1000000000000', '98765432109876.54',
  '1e7', '1.5e5', '+42', '1_234', '0x10', 'abc', '', ' 5', 'Infinity', 'NaN', '123456789012345678901234567890', '99999999999999999999999999999999999', '-99999999999999999999999999999999999', '123456789012345678901234567890123.45'];
const wr = rng(99);
for (let i = 0; i < 1000; i++) amounts.push((wr.chance(0.1) ? '-' : '') + (wr.int(0, 10 ** wr.int(1, 12)) + wr.int(0, 99) / 100).toFixed(2));
for (let i = 0; i < 200; i++) amounts.push(String(wr.int(0, 1000)));
write('words', amounts.map((v) => {
  const finite = !['Infinity', 'NaN'].includes(v);
  return {
    v,
    en: finite ? attempt(() => amountInWordsEn(v)) : null, ne: finite ? attempt(() => amountInWordsNe(v)) : null,
    npr: attempt(() => formatNpr(v)), nprDeva: attempt(() => formatNpr(v, 'devanagari')),
    D: attempt(() => D(v).toString()), fixed2: attempt(() => D(v).toFixed(2)), fixed0: attempt(() => D(v).toFixed(0)),
    rupee: attempt(() => rupee(v).toString()), paisa: attempt(() => paisa(v).toString()),
  };
}));

// ---------------------------------------------------------------------------------------------------------------------
// Optimum CIT on a spread of salaries, regimes and months.
const optimum: Array<{ input: unknown; result: unknown }> = [];
const orng = rng(2083);
const optInputs: PayrollInput[] = [
  baseInput({ base: { BASIC: '120000', ALLOWANCE: '60000' } }), baseInput({ base: { BASIC: '20000', ALLOWANCE: '0' } }),
  baseInput({ base: { BASIC: '120000', ALLOWANCE: '60000' }, declaration: { ...d0, citMonthly: '30000' } }),
  baseInput({ base: { BASIC: '120000', ALLOWANCE: '60000' }, period: { bsYear: 2083, bsMonth: 12, daysInMonth: 30, fiscalYearStart: 2083, monthIndexInFy: 9 },
    ytd: { taxable: '1800000', tdsPaid: '150000', retirementEmployee: '105600', retirementEmployer: '192000', festivalPaid: true } }),
  baseInput({ base: { BASIC: '30000', ALLOWANCE: '400000' }, employee: { ...e0, regime: 'none' } }),
  baseInput({ base: { BASIC: '30000', ALLOWANCE: '40000' }, loans: [{ id: 'L', emi: '60000', remaining: '600000' }], employee: { ...e0, regime: 'none' } }),
];
for (let i = 0; i < 24; i++) {
  const fy = orng.pick([2082, 2083]); const mi = orng.int(1, 12);
  optInputs.push(baseInput({ fy, period: { bsYear: fy, bsMonth: ((mi + 2) % 12) + 1, daysInMonth: 31, fiscalYearStart: fy, monthIndexInFy: mi },
    base: { BASIC: String(orng.int(20000, 400000)), ALLOWANCE: orng.money(200000) }, employee: { ...e0, regime: orng.pick(['ssf', 'pf', 'none'] as const), gender: orng.pick(['male', 'female'] as const) },
    declaration: { ...d0, citMonthly: orng.chance(0.5) ? '0' : orng.money(20000), lifePremium: orng.money(40000), otherRetirementAnnual: orng.chance(0.7) ? '0' : orng.money(100000) } }));
}
for (const input of optInputs) optimum.push({ input: packInput(input), result: attempt(() => optimumCit(input)) });
write('optimum', optimum);

console.log(`payslips ${payslips.length}, formulas ${formulaCases.length}, tax ${taxCases.length}, fractions ${fractions.length}, amounts ${amounts.length}, optimum ${optimum.length}`);
