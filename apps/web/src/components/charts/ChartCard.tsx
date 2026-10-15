import { ActionIcon, Group, Paper, SegmentedControl, Table, Text, Tooltip } from '@mantine/core';
import { IconDownload } from '@tabler/icons-react';
import { useState, type ReactNode } from 'react';

export interface TableView { columns: string[]; rows: Array<Array<string | number | null>> }

/** A chart panel: title, one-line context, legend, CSV download, and a table view of the same numbers. */
export function ChartCard({ title, subtitle, legend, onDownload, table, children, empty }: {
  title: string; subtitle?: ReactNode; legend?: Array<{ label: string; color: string }>; onDownload?: () => void;
  table?: TableView; children: ReactNode; empty?: ReactNode;
}) {
  const [view, setView] = useState<'chart' | 'table'>('chart');
  return (
    <Paper withBorder p="lg" h="100%">
      <Group justify="space-between" align="flex-start" wrap="nowrap" mb="sm" gap="xs">
        <div style={{ minWidth: 0 }}>
          <Text fw={600} size="sm">{title}</Text>
          {subtitle ? <Text size="xs" c="dimmed">{subtitle}</Text> : null}
        </div>
        <Group gap={6} wrap="nowrap" style={{ flex: 'none' }}>
          {table ? <SegmentedControl size="xs" value={view} onChange={(v) => setView(v as 'chart' | 'table')} data={[{ value: 'chart', label: 'Chart' }, { value: 'table', label: 'Table' }]} /> : null}
          {onDownload ? (
            <Tooltip label="Download CSV">
              <ActionIcon variant="subtle" color="gray" onClick={onDownload} aria-label={`Download ${title} as CSV`}><IconDownload size={16} stroke={1.6} /></ActionIcon>
            </Tooltip>
          ) : null}
        </Group>
      </Group>
      {legend && legend.length > 1 && view === 'chart' ? (
        <Group gap="md" mb="xs">
          {legend.map((l) => (
            <Group key={l.label} gap={6} wrap="nowrap">
              <span aria-hidden style={{ width: 10, height: 10, borderRadius: 3, background: l.color, display: 'inline-block' }} />
              <Text size="xs" c="dimmed">{l.label}</Text>
            </Group>
          ))}
        </Group>
      ) : null}
      {empty ?? (view === 'table' && table ? (
        <Table.ScrollContainer minWidth={320} mah={260}>
          <Table striped withTableBorder={false} fz="xs" verticalSpacing={4}>
            <Table.Thead><Table.Tr>{table.columns.map((c, i) => <Table.Th key={c} ta={i ? 'right' : 'left'}>{c}</Table.Th>)}</Table.Tr></Table.Thead>
            <Table.Tbody>
              {table.rows.map((r, i) => (
                <Table.Tr key={i}>{r.map((v, j) => <Table.Td key={j} ta={j ? 'right' : 'left'} className={j ? 'num' : undefined}>{v === null ? '—' : typeof v === 'number' ? v.toLocaleString('en-US') : v}</Table.Td>)}</Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      ) : children)}
    </Paper>
  );
}
