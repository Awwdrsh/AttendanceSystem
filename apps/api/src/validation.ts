import { z } from 'zod';

/** 'YYYY-MM-DD' that is a real calendar date (rejects 2026-13-40). */
export const isoDate = z.string().regex(/^\d{4}-\d{2}-\d{2}$/, 'YYYY-MM-DD').refine((s) => {
  const d = new Date(`${s}T00:00:00Z`);
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === s;
}, 'not a valid date');

/** 'HH:MM' 24-hour. */
export const hhmm = z.string().regex(/^([01]\d|2[0-3]):[0-5]\d$/, 'HH:MM');

/** Decimal amount as a string (money never passes through a float). */
export const money = z.string().regex(/^-?\d{1,12}(\.\d{1,2})?$/, 'amount with up to 2 decimals');

export const uuid = z.uuid();
