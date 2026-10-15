import { Avatar, Badge, Group, SegmentedControl, Stack, Text } from '@mantine/core';
import { useState } from 'react';
import { attendance as attApi, punch as punchApi } from '../../api';
import type { TeamTodayRow } from '../../api/types';
import { DateField } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { StatStrip, type Stat } from '../../components/Kpi';
import { PageHeader } from '../../components/PageHeader';
import { liveStatus, StatusBadge } from '../../components/StatusBadge';
import { fmtDateLong, fmtTime, minutesToHm, todayNpt } from '../../lib/format';
import { useQuery } from '../../lib/useQuery';

const notYetIn = (r: TeamTodayRow) => !r.first_in && r.status !== 'Holiday' && r.status !== 'WeeklyOff' && r.status !== 'OnLeave';
const onLeave = (r: TeamTodayRow) => r.status === 'OnLeave' || r.status === 'HalfDayLeave';

export default function TeamTodayPage() {
  const [date, setDate] = useState<string | null>(todayNpt());
  const [filter, setFilter] = useState('all');
  const q = useQuery(() => attApi.today(date!), [date], { enabled: !!date });
  const all = q.data?.rows ?? [];
  const rows = all.filter((r) => {
    if (filter === 'in') return !!r.first_in;
    if (filter === 'late') return (r.late_min ?? 0) > 0;
    if (filter === 'absent') return r.status === 'Absent' || (!r.status && !r.first_in);
    if (filter === 'notyet') return notYetIn(r);
    if (filter === 'remote') return !!r.remote;
    return true;
  });
  const counts = {
    in: all.filter((r) => !!r.first_in).length,
    late: all.filter((r) => (r.late_min ?? 0) > 0).length,
    absent: all.filter((r) => r.status === 'Absent').length,
    notyet: all.filter(notYetIn).length,
    leave: all.filter(onLeave).length,
    remote: all.filter((r) => !!r.remote).length,
  };
  const isToday = date === todayNpt();
  return (
    <Stack gap="lg">
      <PageHeader title="Who's in"
        subtitle={date ? `${fmtDateLong(date)}${isToday ? '. Statuses update as punches arrive.' : ''}` : 'Pick a date to see your team.'}
        actions={<DateField value={date} onChange={setDate} w={200} />} />
      <StatStrip stats={([
        { key: 'in', label: 'In', value: q.loading && !all.length ? '—' : counts.in, tone: 'ok', hint: all.length ? `of ${all.length} on the team` : undefined },
        { key: 'late', label: 'Late', value: q.loading && !all.length ? '—' : counts.late, tone: 'warn' },
        { key: 'absent', label: 'Absent', value: q.loading && !all.length ? '—' : counts.absent, tone: 'bad' },
        { key: 'notyet', label: isToday ? 'Not yet in' : 'No punch', value: q.loading && !all.length ? '—' : counts.notyet },
        { key: 'remote', label: 'From home', value: q.loading && !all.length ? '—' : counts.remote },
        { key: 'leave', label: 'On leave', value: q.loading && !all.length ? '—' : counts.leave },
      ] as Array<Omit<Stat, 'hint'> & { hint: string | undefined }>).map(({ hint, ...s }): Stat => (hint ? { ...s, hint } : s))} />
      <Group justify="space-between" wrap="wrap" gap="sm">
        <SegmentedControl value={filter} onChange={setFilter}
          data={[{ value: 'all', label: 'Everyone' }, { value: 'in', label: 'In' }, { value: 'late', label: 'Late' }, { value: 'absent', label: 'Absent' }, { value: 'notyet', label: isToday ? 'Not yet in' : 'No punch' }, { value: 'remote', label: 'From home' }]} />
        <Text size="sm" c="dimmed">{rows.length} {rows.length === 1 ? 'person' : 'people'}</Text>
      </Group>
      <DataTable<TeamTodayRow> rows={rows} rowKey={(r) => r.employee_id} loading={q.loading} error={q.error}
        emptyTitle={filter === 'all' ? 'No one on your team yet' : 'No one matches this filter'}
        emptyHint={filter === 'all' ? 'Employees who report to you will appear here.' : 'Switch to Everyone to see the whole team.'}
        columns={[
          { key: 'emp', header: 'Name', render: (r) => <Group gap="sm" wrap="nowrap">{r.photo_id ? <Avatar src={punchApi.photoUrl(r.photo_id)} size={30} radius="xl" alt={`${r.name_en} clock-in photo`} /> : null}<div><Text size="sm" fw={500}>{r.name_en}</Text><Text size="xs" c="dimmed">{r.emp_code}{r.field_place ? ` · at ${r.field_place}` : ''}</Text></div>{r.on_break ? <Badge size="xs" variant="light" color="yellow">On {r.on_break} break</Badge> : null}</Group>, sticky: true },
          { key: 'status', header: 'Status', render: (r) => <StatusBadge status={liveStatus(r.status, { date: date ?? '', today: todayNpt(), firstIn: r.first_in, lastOut: r.last_out }) ?? (r.first_in ? 'Present' : 'NotComputed')} late={r.late_min} remote={r.remote} /> },
          { key: 'in', header: 'First in', align: 'right', render: (r) => <span className="num">{fmtTime(r.first_in)}</span> },
          { key: 'out', header: 'Last out', align: 'right', render: (r) => <span className="num">{fmtTime(r.last_out)}</span> },
          { key: 'breaks', header: 'Breaks', align: 'right', render: (r) => <span className="num">{r.break_min ? minutesToHm(r.break_min) : '—'}</span> },
          { key: 'worked', header: 'Worked', align: 'right', render: (r) => <span className="num">{r.first_in && !r.last_out && date === todayNpt() ? <>{minutesToHm(Math.max(0, Math.round((Date.now() - Date.parse(r.first_in)) / 60_000)))}<Text span size="xs" c="dimmed"> so far</Text></> : minutesToHm(r.worked_min)}</span> },
        ]} />
    </Stack>
  );
}
