import { Checkbox, Group, Loader, Paper, ScrollArea, Table, Text } from '@mantine/core';
import type { ReactNode } from 'react';
import { EmptyState } from './EmptyState';

export interface Column<R> { key: string; header: ReactNode; render?: (row: R) => ReactNode; align?: 'left' | 'right' | 'center'; width?: number | string; sticky?: boolean }

export interface DataTableProps<R> {
  columns: Column<R>[];
  rows: R[];
  rowKey: (row: R) => string;
  loading?: boolean;
  error?: string | null;
  emptyTitle?: string;
  emptyHint?: string;
  selectable?: boolean;
  selected?: Set<string>;
  onSelect?: (next: Set<string>) => void;
  bulkBar?: ReactNode;
  onRowClick?: (row: R) => void;
  maxHeight?: number | string;
  highlight?: (row: R) => boolean;
}

/** Dense data table with sticky header, optional row selection and a bulk action bar (plan §13.2). */
export function DataTable<R>({ columns, rows, rowKey, loading, error, emptyTitle, emptyHint, selectable, selected, onSelect, bulkBar, onRowClick, maxHeight, highlight }: DataTableProps<R>) {
  const sel = selected ?? new Set<string>();
  const allKeys = rows.map(rowKey);
  const allSelected = allKeys.length > 0 && allKeys.every((k) => sel.has(k));
  const toggleAll = () => onSelect?.(allSelected ? new Set() : new Set(allKeys));
  const toggle = (k: string) => { const n = new Set(sel); if (n.has(k)) n.delete(k); else n.add(k); onSelect?.(n); };

  if (error) return <EmptyState title="Could not load" hint={error} />;
  if (!loading && rows.length === 0) return <EmptyState title={emptyTitle ?? 'Nothing here yet.'} {...(emptyHint !== undefined ? { hint: emptyHint } : {})} />;

  return (
    <Paper withBorder radius="lg" style={{ overflow: 'hidden' }}>
      {selectable && sel.size > 0 ? (
        <Group p="xs" bg="var(--mantine-color-brand-light)" justify="space-between">
          <Text size="sm" fw={500}>{sel.size} selected</Text>
          <Group gap="xs">{bulkBar}</Group>
        </Group>
      ) : null}
      <ScrollArea.Autosize mah={maxHeight} type="auto">
        <Table stickyHeader striped={false} withRowBorders miw={Math.min(600, columns.length * 150)}>
          <Table.Thead>
            <Table.Tr>
              {selectable ? <Table.Th w={40}><Checkbox aria-label="Select all" checked={allSelected} indeterminate={!allSelected && sel.size > 0} onChange={toggleAll} /></Table.Th> : null}
              {columns.map((c) => <Table.Th key={c.key} style={{ textAlign: c.align ?? 'left', width: c.width, position: c.sticky ? 'sticky' : undefined, left: c.sticky ? 0 : undefined, background: c.sticky ? 'var(--mantine-color-body)' : undefined, zIndex: c.sticky ? 1 : undefined }}>{c.header}</Table.Th>)}
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {loading && rows.length === 0 ? (
              <Table.Tr><Table.Td colSpan={columns.length + (selectable ? 1 : 0)}><Group justify="center" p="md"><Loader size="sm" /></Group></Table.Td></Table.Tr>
            ) : rows.map((r) => {
              const k = rowKey(r);
              return (
                <Table.Tr key={k} onClick={onRowClick ? () => onRowClick(r) : undefined} style={{ cursor: onRowClick ? 'pointer' : undefined, background: highlight?.(r) ? 'var(--mantine-color-yellow-light)' : undefined }}>
                  {selectable ? <Table.Td onClick={(e) => e.stopPropagation()}><Checkbox aria-label="Select row" checked={sel.has(k)} onChange={() => toggle(k)} /></Table.Td> : null}
                  {columns.map((c) => (
                    <Table.Td key={c.key} style={{ textAlign: c.align ?? 'left', position: c.sticky ? 'sticky' : undefined, left: c.sticky ? 0 : undefined, background: c.sticky ? 'var(--mantine-color-body)' : undefined }}>
                      {c.render ? c.render(r) : String((r as Record<string, unknown>)[c.key] ?? '')}
                    </Table.Td>
                  ))}
                </Table.Tr>
              );
            })}
          </Table.Tbody>
        </Table>
      </ScrollArea.Autosize>
    </Paper>
  );
}
