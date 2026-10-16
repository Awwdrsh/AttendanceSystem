import { Badge } from '@mantine/core';
import type { TableColumn } from '../api/catalog';
import { fmtDate } from '../lib/format';
import { DataTable } from './DataTable';

/** Show one report cell: dates in Bikram Sambat, yes/no as badges, numbers right-aligned. */
export function reportCell(type: TableColumn['type'], v: unknown) {
  if (v === null || v === undefined || v === '') return '—';
  if (type === 'date') return fmtDate(String(v));
  if (type === 'bool') return <Badge size="sm" variant="light" color={v ? 'teal' : 'gray'}>{v ? 'Yes' : 'No'}</Badge>;
  return String(v);
}

/** A `{ columns, rows }` report rendered on screen. */
export function ReportTable({ columns, rows, loading, emptyTitle, maxHeight = 520 }: {
  columns: TableColumn[]; rows: Array<Record<string, unknown>>; loading?: boolean; emptyTitle?: string; maxHeight?: number | string;
}) {
  return (
    <DataTable<Record<string, unknown> & { __i: number }>
      columns={columns.map((c) => ({ key: c.key, header: c.header, align: c.type === 'number' ? 'right' : 'left', render: (r) => reportCell(c.type, r[c.key]) }))}
      rows={rows.map((r, i) => ({ ...r, __i: i }))}
      rowKey={(r) => String(r.__i)}
      loading={!!loading}
      emptyTitle={emptyTitle ?? 'Nothing matches these filters.'}
      maxHeight={maxHeight}
    />
  );
}
