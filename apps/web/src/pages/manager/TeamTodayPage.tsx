import { Group, SegmentedControl, Stack, Text } from '@mantine/core';
import { useState } from 'react';
import { attendance as attApi } from '../../api';
import type { TeamTodayRow } from '../../api/types';
import { BsDatePicker } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { useI18n } from '../../i18n';
import { fmtTime, minutesToHm, todayNpt } from '../../lib/format';
import { useQuery } from '../../lib/useQuery';

export default function TeamTodayPage() {
  const { t, lang, numerals } = useI18n();
  const [date, setDate] = useState<string | null>(todayNpt());
  const [filter, setFilter] = useState('all');
  const q = useQuery(() => attApi.today(date!), [date], { enabled: !!date });
  const rows = (q.data?.rows ?? []).filter((r) => {
    if (filter === 'in') return !!r.first_in;
    if (filter === 'late') return (r.late_min ?? 0) > 0;
    if (filter === 'absent') return r.status === 'Absent' || (!r.status && !r.first_in);
    if (filter === 'notyet') return !r.first_in && r.status !== 'Holiday' && r.status !== 'WeeklyOff' && r.status !== 'OnLeave';
    return true;
  });
  const counts = { in: (q.data?.rows ?? []).filter((r) => !!r.first_in).length, late: (q.data?.rows ?? []).filter((r) => (r.late_min ?? 0) > 0).length, absent: (q.data?.rows ?? []).filter((r) => r.status === 'Absent').length };
  return (
    <Stack>
      <PageHeader title={t('teamToday')} actions={<BsDatePicker value={date} onChange={setDate} w={260} />} />
      <Group>
        <SegmentedControl value={filter} onChange={setFilter} data={[{ value: 'all', label: t('all') }, { value: 'in', label: `In (${counts.in})` }, { value: 'late', label: `${t('late')} (${counts.late})` }, { value: 'absent', label: `${t('absent')} (${counts.absent})` }, { value: 'notyet', label: lang === 'ne' ? 'हाजिर छैन' : 'Not punched' }]} />
        <Text size="xs" c="dimmed">{lang === 'ne' ? 'हरेक मिनेट अद्यावधिक हुन्छ' : 'Statuses update as punches arrive'}</Text>
      </Group>
      <DataTable<TeamTodayRow> rows={rows} rowKey={(r) => r.employee_id} loading={q.loading} error={q.error} emptyTitle="No one matches this filter."
        columns={[
          { key: 'emp', header: t('name'), render: (r) => <><Text size="sm" fw={500}>{r.name_en}</Text><Text size="xs" c="dimmed">{r.emp_code}{r.name_ne ? ` · ${r.name_ne}` : ''}</Text></>, sticky: true },
          { key: 'status', header: t('status'), render: (r) => <StatusBadge status={r.status ?? (r.first_in ? 'Present' : 'NotComputed')} late={r.late_min} /> },
          { key: 'in', header: 'IN', render: (r) => fmtTime(r.first_in, numerals) },
          { key: 'out', header: 'OUT', render: (r) => fmtTime(r.last_out, numerals) },
          { key: 'worked', header: t('worked'), render: (r) => minutesToHm(r.worked_min, numerals) },
        ]} />
    </Stack>
  );
}
