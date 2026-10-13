import { Badge, Button, Group, SegmentedControl, Stack, Text } from '@mantine/core';
import { useState } from 'react';
import { attendance as attApi } from '../../api';
import type { ExceptionRow } from '../../api/types';
import { BsDatePicker } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { useI18n } from '../../i18n';
import { addDays, dualDate, minutesToHm, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

/** Exceptions queue with saved filters (plan §13.2: "Late today", "Missing OUT"). */
export default function ExceptionsPage() {
  const { t, lang, numerals } = useI18n();
  const [from, setFrom] = useState<string | null>(addDays(todayNpt(), -7)); const [to, setTo] = useState<string | null>(todayNpt());
  const [filter, setFilter] = useState('all');
  const q = useQuery(() => attApi.exceptions(from!, to!), [from, to], { enabled: !!from && !!to });
  const [sel, setSel] = useState<Set<string>>(new Set());
  const rows = (q.data?.exceptions ?? []).filter((r) => {
    if (filter === 'missing') return r.status === 'Incomplete';
    if (filter === 'absent') return r.status === 'Absent';
    if (filter === 'ot') return r.flags.some((f) => f.kind.startsWith('OT'));
    if (filter === 'late') return r.late_min > 0;
    return true;
  });
  const recompute = async () => {
    const picked = rows.filter((r) => sel.has(r.id));
    try { for (const r of picked) await attApi.recompute(r.employee_id, r.local_date); toast.ok(`${picked.length} day(s) recomputed.`); setSel(new Set()); await q.reload(); } catch (e) { toast.err(e); }
  };
  return (
    <Stack>
      <PageHeader title={t('exceptions')} subtitle={lang === 'ne' ? 'अपूर्ण, अनुपस्थित र ओभरटाइम झण्डा भएका दिन।' : 'Incomplete, absent and overtime-flagged days that are not locked by payroll.'} actions={<Group gap="xs"><BsDatePicker value={from} onChange={setFrom} w={230} /><BsDatePicker value={to} onChange={setTo} w={230} /></Group>} />
      <SegmentedControl value={filter} onChange={setFilter} data={[{ value: 'all', label: t('all') }, { value: 'missing', label: 'Missing OUT' }, { value: 'absent', label: t('absent') }, { value: 'late', label: t('late') }, { value: 'ot', label: 'OT flags' }]} />
      <DataTable<ExceptionRow> rows={rows} rowKey={(r) => r.id} loading={q.loading} error={q.error} emptyTitle="No exceptions in this range." emptyHint="Good sign: every day computed cleanly." selectable selected={sel} onSelect={setSel} bulkBar={<Button size="xs" onClick={recompute}>Recompute selected</Button>}
        columns={[
          { key: 'emp', header: t('name'), render: (r) => <><Text size="sm" fw={500}>{r.name_en}</Text><Text size="xs" c="dimmed">{r.emp_code}</Text></>, sticky: true },
          { key: 'date', header: t('date'), render: (r) => dualDate(r.local_date, lang, numerals).bs },
          { key: 'status', header: t('status'), render: (r) => <StatusBadge status={r.status} late={r.late_min} /> },
          { key: 'worked', header: t('worked'), render: (r) => minutesToHm(r.worked_min, numerals) },
          { key: 'flags', header: t('flags'), render: (r) => <Group gap={4}>{r.flags.map((f, i) => <Badge key={i} size="xs" variant="outline">{f.kind}{f.minutes ? ` ${f.minutes}m` : ''}</Badge>)}</Group> },
        ]} />
    </Stack>
  );
}
