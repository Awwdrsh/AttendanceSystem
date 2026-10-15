import { Text } from '@mantine/core';
import { people } from '../../api/people';
import { fmtAd, fmtTime, NPT_OFFSET_MIN } from '../../lib/format';
import { useQuery } from '../../lib/useQuery';
import { DataTable } from '../DataTable';

const SOURCE: Record<string, string> = { hr_edit: 'HR edit', change_request: 'Approved request', career_event: 'Career change', import: 'Import', system: 'System' };
const nptDate = (iso: string) => new Date(Date.parse(iso) + NPT_OFFSET_MIN * 60_000).toISOString().slice(0, 10);

/** Every recorded change to the employee's record: field, old value, new value, who and when. */
export function FieldHistory({ employeeId }: { employeeId: string }) {
  const q = useQuery(() => people.history(employeeId), [employeeId]);
  return (
    <DataTable rows={q.data?.history ?? []} rowKey={(r) => r.id} loading={q.loading} error={q.error}
      emptyTitle="No changes recorded yet" emptyHint="Edits to the record, approved change requests and career changes are listed here."
      columns={[
        { key: 'when', header: 'When', render: (r) => <span className="num">{fmtAd(nptDate(r.changed_at))}, {fmtTime(r.changed_at)}</span> },
        { key: 'field', header: 'Field', render: (r) => <Text size="sm" fw={500}>{r.field}</Text> },
        { key: 'old', header: 'From', render: (r) => <Text size="sm" c="dimmed">{r.old_value ?? '—'}</Text> },
        { key: 'new', header: 'To', render: (r) => <Text size="sm">{r.new_value ?? '—'}</Text> },
        { key: 'src', header: 'How', render: (r) => SOURCE[r.source] ?? r.source },
        { key: 'by', header: 'By', render: (r) => r.changed_by_name ?? 'System' },
      ]} />
  );
}
