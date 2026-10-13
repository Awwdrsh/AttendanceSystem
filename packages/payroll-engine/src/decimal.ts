import { Decimal } from 'decimal.js';

/** Money arithmetic never goes through JS floats (plan §11.7). 30 significant digits, half-up by default. */
Decimal.set({ precision: 30, rounding: Decimal.ROUND_HALF_UP });

export { Decimal };
export type Num = Decimal.Value;

export const D = (v: Num): Decimal => new Decimal(v);
export const ZERO = new Decimal(0);

/** Half-up to the rupee (plan §11.7: statutory and net). */
export const rupee = (v: Num): Decimal => new Decimal(v).toDecimalPlaces(0, Decimal.ROUND_HALF_UP);
/** Half-up to the paisa (internal lines). */
export const paisa = (v: Num): Decimal => new Decimal(v).toDecimalPlaces(2, Decimal.ROUND_HALF_UP);
/** 'NNN.NN' string for storage in numeric(14,2). */
export const money = (v: Num): string => paisa(v).toFixed(2);

export const sum = (xs: Iterable<Num>): Decimal => { let t = ZERO; for (const x of xs) t = t.plus(x); return t; };
export const max0 = (v: Num): Decimal => Decimal.max(0, v);
