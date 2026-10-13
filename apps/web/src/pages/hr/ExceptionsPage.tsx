import { Badge, Button, Group, SegmentedControl, Stack, Text } from '@mantine/core';
import { IconRefresh } from '@tabler/icons-react';
import { useState } from 'react';
import { attendance as attApi } from '../../api';
import type { ExceptionRow, Flag } from '../../api/types';
import { DateField } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { addDays, fmtDateLong, minutesToHm, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const FLAG_LABEL: Record<string, string> = {
  AutoClockOut: 'Auto clock-out', EarlyOut: 'Early out', HalfDayLeave: 'Half-day leave', HolidayWork: 'Worked on holiday', Late: 'Late',
  MissingOut: 'Missing clock-out', OTCapExceeded: 'Over overtime cap', OTPreapprovalMissing: 'Overtime not pre-approved', UnpaidLeave: 'Unpaid leave', UnpairedPunch: 'Unpaired punch',
};
const flagText = (f: Flag) => `${FLAG_LABEL[f.kind] ?? f.kind}${f.minutes ? `, ${minutesToHm(f.minutes)}` : ''}`;

/** Exceptions queue with saved filters (plan §13.2: "Late today", "Missing OUT"). */
export default function ExceptionsPage() {
  const [from, setFrom] = useState<string | null>(addDays(todayNpt(), -7)); const [to, setTo] = useState<string | null>(todayNpt());
  const [filter, setFilter] = useState('all');
  const q = useQuery(() => attApi.exceptions(from!, to!), [from, to], { enabled: !!from && !!to });
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const today = todayNpt();
  // Today's open days are people still at work, not missing punches; leave them out until the day is over.
  const raw = q.data?.exceptions ?? [];
  const all = raw.filter((r) => !(r.local_date === today && r.status === 'Incomplete'));
  const stillIn = raw.length - all.length;
  const match = (r: ExceptionRow, f: string) => {
    if (f === 'missing') return r.status === 'Incomplete';
    if (f === 'absent') return r.status === 'Absent';
    if (f === 'ot') return r.flags.some((x) => x.kind.startsWith('OT'));
    if (f === 'late') return r.late_min > 0;
    return true;
  };
  const rows = all.filter((r) => match(r, filter));
  const count = (f: string) => all.filter((r) => match(r, f)).length;
  const recompute = async () => {
    const picked = rows.filter((r) => sel.has(r.id));
    setBusy(true);
    try { for (const r of picked) await attApi.recompute(r.employee_id, r.local_date); toast.ok(`${picked.length} ${picked.length === 1 ? 'day' : 'days'} recalculated.`); setSel(new Set()); await q.reload(); } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Stack gap="md">
      <PageHeader title="Exceptions" subtitle={`Days with a missing punch, an absence or an overtime flag that payroll has not locked yet. Fix the punch, then recalculate the day.${stillIn > 0 ? ` ${stillIn} people still at work today are not listed.` : ''}`}
        actions={<Group gap="xs" wrap="wrap" align="center">
          <DateField value={from} onChange={setFrom} w={170} placeholder="From" {...(to ? { maxDate: to } : {})} />
          <Text size="sm" c="dimmed">to</Text>
          <DateField value={to} onChange={setTo} w={170} placeholder="To" {...(from ? { minDate: from } : {})} />
        </Group>} />
      <Group justify="space-between" wrap="wrap" gap="sm">
        <SegmentedControl value={filter} onChange={setFilter} data={[
          { value: 'all', label: `All ${count('all')}` },
          { value: 'missing', label: `Missing clock-out ${count('missing')}` },
          { value: 'absent', label: `Absent ${count('absent')}` },
          { value: 'late', label: `Late ${count('late')}` },
          { value: 'ot', label: `Overtime flags ${count('ot')}` },
        ]} />
        {!from || !to ? <Text size="sm" c="dimmed">Pick both dates to load exceptions.</Text> : null}
      </Group>
      <DataTable<ExceptionRow> rows={rows} rowKey={(r) => r.id} loading={q.loading} error={q.error}
        emptyTitle={filter === 'all' ? 'No exceptions in this range' : 'Nothing matches this filter'}
        emptyHint={filter === 'all' ? 'Every day in this range calculated cleanly.' : 'Switch to All to see every exception in the range.'}
        selectable selected={sel} onSelect={setSel}
        bulkBar={<Button size="xs" leftSection={<IconRefresh size={14} stroke={1.8} />} loading={busy} onClick={recompute}>Recalculate selected</Button>}
        columns={[
          { key: 'emp', header: 'Employee', render: (r) => <><Text size="sm" fw={500}>{r.name_en}</Text><Text size="xs" c="dimmed">{r.emp_code}</Text></>, sticky: true },
          { key: 'date', header: 'Date', render: (r) => <Text size="sm" style={{ whiteSpace: 'nowrap' }}>{fmtDateLong(r.local_date)}</Text> },
          { key: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} late={r.late_min} /> },
          { key: 'worked', header: 'Worked', align: 'right', render: (r) => <span className="num">{minutesToHm(r.worked_min)}</span> },
          { key: 'flags', header: 'Flags', render: (r) => r.flags.length ? <Group gap={4}>{r.flags.map((f, i) => <Badge key={i} size="sm" variant="default" fw={500}>{flagText(f)}</Badge>)}</Group> : <Text size="sm" c="dimmed">None</Text> },
        ]} />
    </Stack>
  );
}
