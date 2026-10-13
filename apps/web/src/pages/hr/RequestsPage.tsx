import { Button, Select, Stack, Tabs, Text } from '@mantine/core';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { leave as leaveApi, ot as otApi, regularisation as regApi } from '../../api';
import type { LeaveRequest, OtRequest, RegularisationRequest } from '../../api/types';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { DataTable } from '../../components/DataTable';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { fmtDateShort, fmtTime, minutesToHm } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const STATUS_OPTS = [{ value: 'pending', label: 'Pending' }, { value: 'approved', label: 'Approved' }, { value: 'rejected', label: 'Rejected' }, { value: 'cancelled', label: 'Cancelled' }];

const nameCell = (r: { name_en: string; emp_code: string }) => <><Text size="sm" fw={500}>{r.name_en}</Text><Text size="xs" c="dimmed">{r.emp_code}</Text></>;

function kindLabel(r: RegularisationRequest): string {
  const k = r.kind.replace(/_/g, ' ');
  const base = k.charAt(0).toUpperCase() + k.slice(1);
  if (!r.punch_time) return base;
  const dir = r.punch_direction?.toLowerCase() === 'in' ? 'clock-in' : r.punch_direction ? 'clock-out' : '';
  return `${base}, ${dir ? `${dir} ` : ''}at ${fmtTime(r.punch_time)}`;
}

export default function RequestsPage() {
  const nav = useNavigate();
  const [status, setStatus] = useState<string | null>('pending');
  const [cancelRow, setCancelRow] = useState<LeaveRequest | null>(null);
  const leaves = useQuery(() => leaveApi.requests({ scope: 'all', status }), [status]);
  const regs = useQuery(() => regApi.list({ scope: 'all', status }), [status]);
  const ots = useQuery(() => otApi.requests({ scope: 'all', status }), [status]);
  const statusWord = status ? `${STATUS_OPTS.find((s) => s.value === status)?.label.toLowerCase() ?? status} ` : '';
  return (
    <Stack gap="md">
      <PageHeader title="Requests" subtitle="Every leave, attendance correction and overtime request across the company. Decisions are made from the Approvals inbox so each one is signed."
        actions={<>
          <Select value={status} onChange={setStatus} data={STATUS_OPTS} clearable placeholder="Any status" w={160} aria-label="Filter by status" />
          <Button variant="default" onClick={() => nav('/manager/approvals')}>Open approvals</Button>
        </>} />
      <Tabs defaultValue="leave">
        <Tabs.List>
          <Tabs.Tab value="leave">Leave <Text span c="dimmed" size="sm" className="num">{leaves.data?.requests.length ?? 0}</Text></Tabs.Tab>
          <Tabs.Tab value="reg">Attendance corrections <Text span c="dimmed" size="sm" className="num">{regs.data?.requests.length ?? 0}</Text></Tabs.Tab>
          <Tabs.Tab value="ot">Overtime <Text span c="dimmed" size="sm" className="num">{ots.data?.requests.length ?? 0}</Text></Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="leave" pt="md">
          <DataTable<LeaveRequest> rows={leaves.data?.requests ?? []} rowKey={(r) => r.id} loading={leaves.loading} error={leaves.error}
            emptyTitle={`No ${statusWord}leave requests`} emptyHint="Try another status filter."
            columns={[
              { key: 'emp', header: 'Employee', render: nameCell },
              { key: 'type', header: 'Leave type', render: (r) => r.type_name },
              { key: 'dates', header: 'Dates', render: (r) => `${fmtDateShort(r.from_date)}${r.to_date !== r.from_date ? ` to ${fmtDateShort(r.to_date)}` : ''}` },
              { key: 'days', header: 'Days', render: (r) => <span className="num">{r.days_requested}</span>, align: 'right' },
              { key: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} /> },
              { key: 'a', header: '', align: 'right', render: (r) => r.status === 'pending' || r.status === 'approved' ? <Button size="xs" variant="subtle" color="red" onClick={() => setCancelRow(r)}>Cancel leave</Button> : null },
            ]} />
        </Tabs.Panel>
        <Tabs.Panel value="reg" pt="md">
          <DataTable<RegularisationRequest> rows={regs.data?.requests ?? []} rowKey={(r) => r.id} loading={regs.loading} error={regs.error}
            emptyTitle={`No ${statusWord}attendance corrections`} emptyHint="Try another status filter."
            columns={[
              { key: 'emp', header: 'Employee', render: nameCell },
              { key: 'date', header: 'Date', render: (r) => fmtDateShort(r.local_date) },
              { key: 'kind', header: 'Correction', render: kindLabel },
              { key: 'reason', header: 'Reason', render: (r) => <Text size="sm" c="dimmed" lineClamp={2} maw={320}>{r.reason}</Text> },
              { key: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} /> },
            ]} />
        </Tabs.Panel>
        <Tabs.Panel value="ot" pt="md">
          <DataTable<OtRequest> rows={ots.data?.requests ?? []} rowKey={(r) => r.id} loading={ots.loading} error={ots.error}
            emptyTitle={`No ${statusWord}overtime requests`} emptyHint="Try another status filter."
            columns={[
              { key: 'emp', header: 'Employee', render: nameCell },
              { key: 'date', header: 'Date', render: (r) => fmtDateShort(r.local_date) },
              { key: 'req', header: 'Requested', align: 'right', render: (r) => <span className="num">{minutesToHm(r.requested_min)}</span> },
              { key: 'apr', header: 'Approved', align: 'right', render: (r) => <span className="num">{r.approved_min === null ? '—' : minutesToHm(r.approved_min)}</span> },
              { key: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} /> },
            ]} />
        </Tabs.Panel>
      </Tabs>
      <ConfirmDialog opened={!!cancelRow} onClose={() => setCancelRow(null)} title="Cancel leave?" confirmLabel="Cancel leave"
        message={cancelRow ? `${cancelRow.name_en}'s ${cancelRow.type_name.toLowerCase()} for ${fmtDateShort(cancelRow.from_date)}${cancelRow.to_date !== cancelRow.from_date ? ` to ${fmtDateShort(cancelRow.to_date)}` : ''} will be cancelled and the days returned to their balance.` : ''}
        onConfirm={async () => { if (!cancelRow) return; try { await leaveApi.cancel(cancelRow.id, 'cancelled by HR'); toast.ok('Leave cancelled.'); await leaves.reload(); } catch (e) { toast.err(e); } }} />
    </Stack>
  );
}
