import { Button, Group, SegmentedControl, Stack, Text } from '@mantine/core';
import { IconDownload, IconReceiptRefund } from '@tabler/icons-react';
import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { expenses as api, type ClaimRow, type ClaimStatus } from '../../api/expenses';
import { useAuth } from '../../auth/AuthProvider';
import { DataTable } from '../../components/DataTable';
import { EmptyState } from '../../components/EmptyState';
import { StatStrip } from '../../components/Kpi';
import { SectionTitle } from '../../components/PageHeader';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { ClaimBadge, ClaimDrawer, money, payoutLabel, span } from './shared';

type Filter = 'pending' | 'approved' | 'paid' | 'all';

/** Claims of the team (managers) or the company (HR): review with receipts, approve less with a reason, see what payroll will pay. */
export default function ClaimsReviewPage() {
  const { hasRole } = useAuth();
  const [params, setParams] = useSearchParams();
  const [filter, setFilter] = useState<Filter>('pending');
  const q = useQuery(() => api.claims({ scope: hasRole('hr') ? 'all' : 'team', ...(filter === 'all' ? {} : { status: filter as ClaimStatus }) }), [filter]);
  const pending = useQuery(() => (hasRole('hr') ? api.pendingPayment() : Promise.resolve(null)), []);
  const open = params.get('open');
  const setOpen = (id: string | null) => { const p = new URLSearchParams(params); if (id) p.set('open', id); else p.delete('open'); setParams(p, { replace: true }); };
  const rows = q.data?.claims ?? [];
  const payRows = pending.data?.rows ?? [];
  const toPay = payRows.filter((r) => r.kind === 'Pay').reduce((a, r) => a + Number(r.amount), 0);
  const toRecover = payRows.filter((r) => r.kind === 'Recover').reduce((a, r) => a + Number(r.amount), 0);

  return (
    <Stack gap="lg">
      <SectionTitle hint={hasRole('hr') ? 'Every claim in the company. After the manager approves, HR gives the final approval; approved amounts are paid in the next pay run.' : 'Claims from your team waiting for you, and what happened to earlier ones.'}
        actions={<Group gap="xs" wrap="wrap">
          <SegmentedControl size="xs" value={filter} onChange={(v) => setFilter(v as Filter)} data={[{ value: 'pending', label: 'Waiting' }, { value: 'approved', label: 'Approved' }, { value: 'paid', label: 'Paid' }, { value: 'all', label: 'All' }]} />
        </Group>}>Expense claims</SectionTitle>
      {hasRole('hr') ? (
        <StatStrip stats={[
          { key: 'p', label: 'To pay in the next pay run', value: money(toPay), hint: 'Approved claims and travel settlements' },
          { key: 'r', label: 'To recover', value: money(toRecover), hint: 'Unspent travel advances' },
          { key: 'n', label: 'Items', value: payRows.length, hint: <Button variant="subtle" size="compact-xs" leftSection={<IconDownload size={12} />} onClick={() => api.downloadReport('pending-payment').catch((e) => toast.err(e, 'Download failed'))}>Download list</Button> },
        ]} />
      ) : null}
      {!q.loading && rows.length === 0 ? (
        <EmptyState icon={<IconReceiptRefund size={20} stroke={1.6} />} title={filter === 'pending' ? 'Nothing waiting for a decision' : 'No claims here'}
          hint={filter === 'pending' ? 'New claims from your team will show here and in Approvals.' : 'Try another filter.'} />
      ) : (
        <DataTable<ClaimRow> rows={rows} rowKey={(c) => c.id} loading={q.loading} error={q.error} onRowClick={(c) => setOpen(c.id)} columns={[
          { key: 'e', header: 'Employee', render: (c) => <><Text size="sm" fw={500}>{c.employeeName}</Text><Text size="xs" c="dimmed">{c.empCode}</Text></> },
          { key: 't', header: 'Claim', render: (c) => <><Text size="sm">{c.title}</Text><Text size="xs" c="dimmed">{c.firstDate && c.lastDate ? span(c.firstDate, c.lastDate) : ''} · {c.items} {c.items === 1 ? 'item' : 'items'}</Text></> },
          { key: 'a', header: 'Amount', align: 'right', render: (c) => money(c.totalApproved ?? c.totalClaimed) },
          { key: 's', header: 'Status', render: (c) => <ClaimBadge status={c.status} /> },
          { key: 'p', header: 'Payment', render: (c) => <Text size="sm" c="dimmed">{c.payoutStatus ? payoutLabel({ status: c.payoutStatus as never, bsYear: c.payBsYear, bsMonth: c.payBsMonth, code: 'EXP_REIMB' }) : c.paymentMethod === 'cash' ? 'Paid outside payroll' : '—'}</Text> },
        ]} />
      )}
      <ClaimDrawer id={open} onClose={() => setOpen(null)} onChanged={() => { void q.reload(); void pending.reload(); }} />
    </Stack>
  );
}
