import { describe, expect, it } from 'vitest';
import { addDays, dualDate, fiscalLabel, fmtDateLong, fmtMoney, fmtTime, groupDigits, minutesToHm } from './format';

describe('format helpers', () => {
  it('groups digits lakh/crore style and international style', () => {
    expect(groupDigits('1234567')).toBe('12,34,567');
    expect(groupDigits('123')).toBe('123');
    expect(groupDigits('1234567', 'intl')).toBe('1,234,567');
  });
  it('formats money from decimal strings without float drift', () => {
    expect(fmtMoney('2163.46')).toBe('NPR 2,163.46');
    expect(fmtMoney('123456789.5')).toBe('NPR 12,34,56,789.50');
    expect(fmtMoney('-40000')).toBe('−NPR 40,000.00');
    expect(fmtMoney(null)).toBe('—');
  });
  it('renders minutes as hours and minutes', () => {
    expect(minutesToHm(503)).toBe('8 h 23 m');
    expect(minutesToHm(60)).toBe('1 h');
    expect(minutesToHm(5)).toBe('5 m');
  });
  it('shows Nepal local time for a UTC instant', () => {
    expect(fmtTime('2026-09-21T04:32:03.000Z')).toBe('10:17');
  });
  it('dual dates follow the BS table (plan §10.5 example F)', () => {
    const d = dualDate('2026-08-28');
    expect(d.bs).toBe('2083-05-12');
    expect(d.bsLong).toBe('12 Bhadra 2083');
    expect(d.ad).toBe('28 Aug 2026');
  });
  it('long English dates', () => {
    expect(fmtDateLong('2026-10-13')).toBe('Tue, 27 Ashwin 2083');
    expect(fmtDateLong('2026-10-14')).toBe('Wed, 28 Ashwin 2083');
  });
  it('fiscal year and date arithmetic', () => {
    expect(fiscalLabel('2026-08-28')).toBe('2083/84');
    expect(fiscalLabel('2026-07-16')).toBe('2082/83');
    expect(addDays('2026-12-31', 1)).toBe('2027-01-01');
  });
});
