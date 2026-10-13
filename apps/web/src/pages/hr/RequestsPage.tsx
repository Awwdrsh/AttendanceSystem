import { Button, Group, Select, Stack, Tabs, Text } from '@mantine/core';
import { useState } from 'react';
import { leave as leaveApi, ot as otApi, regularisation as regApi } from '../../api';
import type { LeaveRequest, OtRequest, RegularisationRequest } from '../../api/types';
import { DataTable } from '../../components/DataTable';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { useI18n } from '../../i18n';
import { dualDate, fmtTime, minutesToHm } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

export default function RequestsPage() {
  const { t, lang, numerals } = useI18n();
  const [status, setStatus] = useState<string | null>('pending');
  const leaves = useQuery(() => leaveApi.requests({ scope: 'all', status }), [status]);
  const regs = useQuery(() => regApi.list({ scope: 'all', status }), [status]);
  const ots = useQuery(() => otApi.requests({ scope: 'all', status }), [status]);
  return (
    <Stack>
      <PageHeader title={t('requests')} subtitle="All leave, correction and overtime requests across the company. Approve from the Approvals inbox." actions={<Select value={status} onChange={setStatus} data={['pending', 'approved', 'rejected', 'cancelled']} clearable placeholder="Any status" w={160} />} />
      <Tabs defaultValue="leave">
        <Tabs.List><Tabs.Tab value="leave">{t('leave')} ({leaves.data?.requests.length ?? 0})</Tabs.Tab><Tabs.Tab value="reg">Corrections ({regs.data?.requests.length ?? 0})</Tabs.Tab><Tabs.Tab value="ot">{t('overtime')} ({ots.data?.requests.length ?? 0})</Tabs.Tab></Tabs.List>
        <Tabs.Panel value="leave" pt="md">
          <DataTable<LeaveRequest> rows={leaves.data?.requests ?? []} rowKey={(r) => r.id} loading={leaves.loading} error={leaves.error} emptyTitle="No leave requests."
            columns={[{ key: 'emp', header: t('name'), render: (r) => <><Text size="sm" fw={500}>{r.name_en}</Text><Text size="xs" c="dimmed">{r.emp_code}</Text></> }, { key: 'type', header: t('leave'), render: (r) => r.type_name },
              { key: 'dates', header: t('date'), render: (r) => `${dualDate(r.from_date, lang, numerals).bs}${r.to_date !== r.from_date ? ` → ${dualDate(r.to_date, lang, numerals).bs}` : ''}` }, { key: 'days', header: t('days'), render: (r) => r.days_requested, align: 'right' },
              { key: 'status', header: t('status'), render: (r) => <StatusBadge status={r.status} /> }, { key: 'a', header: '', render: (r) => r.status === 'pending' || r.status === 'approved' ? <Button size="xs" variant="subtle" color="red" onClick={async () => { try { await leaveApi.cancel(r.id, 'cancelled by HR'); await leaves.reload(); } catch (e) { toast.err(e); } }}>{t('cancel')}</Button> : null }]} />
        </Tabs.Panel>
        <Tabs.Panel value="reg" pt="md">
          <DataTable<RegularisationRequest> rows={regs.data?.requests ?? []} rowKey={(r) => r.id} loading={regs.loading} error={regs.error} emptyTitle="No correction requests."
            columns={[{ key: 'emp', header: t('name'), render: (r) => <><Text size="sm" fw={500}>{r.name_en}</Text><Text size="xs" c="dimmed">{r.emp_code}</Text></> }, { key: 'date', header: t('date'), render: (r) => dualDate(r.local_date, lang, numerals).bs },
              { key: 'kind', header: 'Kind', render: (r) => `${r.kind.replace('_', ' ')}${r.punch_time ? ` ${fmtTime(r.punch_time, numerals)} ${r.punch_direction?.toUpperCase() ?? ''}` : ''}` }, { key: 'reason', header: t('reason'), render: (r) => <Text size="xs">{r.reason}</Text> },
              { key: 'status', header: t('status'), render: (r) => <StatusBadge status={r.status} /> }]} />
        </Tabs.Panel>
        <Tabs.Panel value="ot" pt="md">
          <DataTable<OtRequest> rows={ots.data?.requests ?? []} rowKey={(r) => r.id} loading={ots.loading} error={ots.error} emptyTitle="No overtime requests."
            columns={[{ key: 'emp', header: t('name'), render: (r) => <><Text size="sm" fw={500}>{r.name_en}</Text><Text size="xs" c="dimmed">{r.emp_code}</Text></> }, { key: 'date', header: t('date'), render: (r) => dualDate(r.local_date, lang, numerals).bs },
              { key: 'req', header: 'Requested', render: (r) => minutesToHm(r.requested_min, numerals) }, { key: 'apr', header: 'Approved', render: (r) => r.approved_min === null ? '—' : minutesToHm(r.approved_min, numerals) }, { key: 'status', header: t('status'), render: (r) => <StatusBadge status={r.status} /> }]} />
        </Tabs.Panel>
      </Tabs>
      <Group><Text size="xs" c="dimmed">{lang === 'ne' ? 'निर्णय "स्वीकृति" पृष्ठबाट गर्नुहोस्।' : 'Decisions are made from the Approvals inbox so every action is signed.'}</Text></Group>
    </Stack>
  );
}
