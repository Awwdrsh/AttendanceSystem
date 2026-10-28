import { describe, expect, it } from 'vitest';
import { normalizeError, qs } from './client';

describe('query string builder', () => {
  it('drops undefined, null and empty values and encodes the rest', () => {
    expect(qs({ a: 1, b: 'x y', c: undefined, d: null, e: '', f: false })).toBe('?a=1&b=x+y&f=false');
    expect(qs()).toBe('');
    expect(qs({ a: undefined })).toBe('');
  });
});

describe('error normalisation', () => {
  it('uses the error field and keeps structured bodies as details', () => {
    const e = normalizeError(409, { error: 'already exists', constraint: 'x' });
    expect(e.status).toBe(409);
    expect(e.message).toBe('already exists');
    expect(e.details).toEqual({ error: 'already exists', constraint: 'x' });
  });
  it('joins validation issues with their paths', () => {
    const e = normalizeError(400, {
      error: 'validation',
      issues: [
        { path: 'from', message: 'YYYY-MM-DD' },
        { path: '', message: 'bad' },
      ],
    });
    expect(e.message).toBe('from: YYYY-MM-DD; bad');
    expect(e.issues).toHaveLength(2);
  });
  it('keeps row errors from a CSV dry run even without an error field', () => {
    const body = { imported: 0, valid: 1, errors: [{ row: 3, message: 'pan: PAN must be 9 digits' }] };
    const e = normalizeError(400, body);
    expect(e.message).toBe('Request failed (400)');
    expect(e.details).toBe(body);
  });
  it('surfaces the leave split suggestion and plain-text bodies', () => {
    expect(normalizeError(400, { error: 'not enough', suggestion: { paidDays: 1, lwpDays: 2 } }).suggestion).toEqual({
      paidDays: 1,
      lwpDays: 2,
    });
    expect(normalizeError(503, 'ERROR: database unavailable').message).toBe('ERROR: database unavailable');
    expect(normalizeError(500, null).message).toBe('Request failed (500)');
  });
});
