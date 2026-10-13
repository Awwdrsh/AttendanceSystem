import type { Response } from 'express';

/** RFC 4180 CSV with a UTF-8 BOM so Excel opens Devanagari correctly. */
export function toCsv(columns: Array<{ key: string; header: string }>, rows: Array<Record<string, unknown>>): string {
  const esc = (v: unknown): string => {
    if (v === null || v === undefined) return '';
    let s = v instanceof Date ? v.toISOString() : String(v);
    // Spreadsheet formula injection: a leading = + - @ or tab/CR makes Excel evaluate the cell; neutralise it.
    if (/^[=+\-@\t\r]/.test(s)) s = `'${s}`;
    return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = [columns.map((c) => esc(c.header)).join(',')];
  for (const r of rows) lines.push(columns.map((c) => esc(r[c.key])).join(','));
  return '﻿' + lines.join('\r\n') + '\r\n';
}

export function sendReport(res: Response, format: string | undefined, filename: string, columns: Array<{ key: string; header: string }>, rows: Array<Record<string, unknown>>): void {
  if (format === 'csv') {
    res.setHeader('Content-Type', 'text/csv; charset=utf-8');
    res.setHeader('Content-Disposition', `attachment; filename="${filename}.csv"`);
    res.send(toCsv(columns, rows));
    return;
  }
  res.json({ columns, rows });
}

/** 'HH:MM' in Nepal time for an instant, or ''. */
export function nptHHMM(d: Date | null): string {
  return d ? new Date(d.getTime() + 345 * 60_000).toISOString().slice(11, 16) : '';
}
export const hours = (min: number | null | undefined): string => (min ? (min / 60).toFixed(2) : '0.00');
