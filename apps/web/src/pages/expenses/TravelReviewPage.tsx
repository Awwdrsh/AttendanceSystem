import { Button, Group, SegmentedControl, Stack, Text } from '@mantine/core';
import { IconDownload, IconPlane } from '@tabler/icons-react';
import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { expenses as api, type TripRow, type TripStatus } from '../../api/expenses';
import { useAuth } from '../../auth/AuthProvider';
import { DataTable } from '../../components/DataTable';
import { EmptyState } from '../../components/EmptyState';
import { StatStrip } from '../../components/Kpi';
import { SectionTitle } from '../../components/PageHeader';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { DEST_LABEL, money, span, TripBadge, TripDrawer } from './shared';

type Filter = 'pending' | 'approved' | 'settling' | 'settled' | 'all';

/** Trips of the team (managers) or company (HR): approve trips and advances, then approve settlements. */
export default function TravelReviewPage() {
  const { hasRole } = useAuth();
  const [params, setParams] = useSearchParams();
  const [filter, setFilter] = useState<Filter>('pending');
  const q = useQuery(() => api.trips({ scope: hasRole('hr') ? 'all' : 'team', ...(filter === 'all' ? {} : { status: filter as TripStatus }) }), [filter]);
  const adv = useQuery(() => (hasRole('hr') ? api.advances() : Promise.resolve(null)), []);
  const open = params.get('open');
  const setOpen = (id: string | null) => { const p = new URLSearchParams(params); if (id) p.set('open', id); else p.delete('open'); setParams(p, { replace: true }); };
  const rows = q.data?.trips ?? [];
  const advRows = adv.data?.rows ?? [];

  return (
    <Stack gap="lg">
      <SectionTitle hint="Trips are approved by the manager and then HR. After the trip, the traveller settles it with bills; any unspent advance is recovered from salary."
        actions={<SegmentedControl size="xs" value={filter} onChange={(v) => setFilter(v as Filter)}
          data={[{ value: 'pending', label: 'To approve' }, { value: 'approved', label: 'Approved' }, { value: 'settling', label: 'Settlements' }, { value: 'settled', label: 'Settled' }, { value: 'all', label: 'All' }]} />}>Travel</SectionTitle>
      {hasRole('hr') ? (
        <StatStrip stats={[
          { key: 'a', label: 'Advances not yet settled', value: money(advRows.reduce((a, r) => a + Number(r.advance ?? 0), 0)) },
          { key: 'd', label: 'Trips with an open advance', value: advRows.length, hint: <Button variant="subtle" size="compact-xs" leftSection={<IconDownload size={12} />} onClick={() => api.downloadReport('advances').catch((e) => toast.err(e, 'Download failed'))}>Download list</Button> },
        ]} />
      ) : null}
      {!q.loading && rows.length === 0 ? (
        <EmptyState icon={<IconPlane size={20} stroke={1.6} />} title={filter === 'pending' ? 'No trips waiting for a decision' : 'No trips here'} hint="Try another filter." />
      ) : (
        <DataTable<TripRow> rows={rows} rowKey={(t) => t.id} loading={q.loading} error={q.error} onRowClick={(t) => setOpen(t.id)} columns={[
          { key: 'e', header: 'Employee', render: (t) => <><Text size="sm" fw={500}>{t.employeeName}</Text><Text size="xs" c="dimmed">{t.empCode}</Text></> },
          { key: 'd', header: 'Trip', render: (t) => <><Text size="sm">{t.destination}</Text><Text size="xs" c="dimmed">{DEST_LABEL[t.destinationCategory]} · {span(t.fromDate, t.toDate)}</Text></> },
          { key: 'a', header: 'Allowance', align: 'right', render: (t) => money(t.daAmount) },
          { key: 'v', header: 'Advance', align: 'right', render: (t) => (Number(t.advanceRequested) > 0 ? money(t.advanceApproved ?? t.advanceRequested) : '—') },
          { key: 's', header: 'Status', render: (t) => <Group gap={6} wrap="nowrap"><TripBadge status={t.status} />{t.settlement && t.status !== 'settled' && t.settlement.status === 'rejected' ? <Text size="xs" c="dimmed">settlement returned</Text> : null}</Group> },
        ]} />
      )}
      <TripDrawer id={open} onClose={() => setOpen(null)} onChanged={() => { void q.reload(); void adv.reload(); }} />
    </Stack>
  );
}
