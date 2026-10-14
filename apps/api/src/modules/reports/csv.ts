import type { Response } from 'express';

/**
 * One CSV cell, RFC 4180 quoted. Spreadsheet formula injection: a text cell starting with = + - @ tab or CR is
 * evaluated by Excel, so it is prefixed with an apostrophe. Plain numbers (including negatives) are left alone.
 */
export function csvCell(v: unknown): string {
  if (v === null || v === undefined) return '';
  let s = v instanceof Date ? v.toISOString() : String(v);
  if (/^[=+\-@\t\r]/.test(s) && !/^-?\d+(\.\d+)?$/.test(s)) s = `'${s}`;
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** RFC 4180 CSV with a UTF-8 BOM so Excel opens Devanagari correctly. */
export function toCsv(columns: Array<{ key: string; header: string }>, rows: Array<Record<string, unknown>>): string {
  const lines = [columns.map((c) => csvCell(c.header)).join(',')];
  for (const r of rows) lines.push(columns.map((c) => csvCell(r[c.key])).join(','));
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
