import { Button, Group, Modal, NumberInput, Select, Stack, Text, Textarea } from '@mantine/core';
import { IconCheck, IconDownload, IconX } from '@tabler/icons-react';
import { useState } from 'react';
import { ot as otApi } from '../../api';
import type { OtSlip } from '../../api/types';
import { DateField } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { addDays, fmtAd, fmtDateLong, minutesToHm, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const STATUS_OPTS = [{ value: 'detected', label: 'Needs review' }, { value: 'approved', label: 'Approved' }, { value: 'rejected', label: 'Rejected' }, { value: 'posted', label: 'Posted to payroll' }, { value: 'comp_off', label: 'Comp-off' }];
const dayType = (t: string) => { const s = t.replace(/_/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase(); return s.charAt(0).toUpperCase() + s.slice(1); };

export default function OtSlipsPage() {
  const [from, setFrom] = useState<string | null>(addDays(todayNpt(), -30)); const [to, setTo] = useState<string | null>(todayNpt()); const [status, setStatus] = useState<string | null>('detected');
  const q = useQuery(() => otApi.slips({ from, to, status }), [from, to, status]);
  const [approve, setApprove] = useState<OtSlip | null>(null); const [approvedMin, setApprovedMin] = useState<number | string>(0);
  const [reject, setReject] = useState<OtSlip | null>(null); const [reason, setReason] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const slips = q.data?.slips ?? [];
  return (
    <Stack gap="md">
      <PageHeader title="Overtime review" subtitle="Only approved hours reach payroll. Where policy allows, convert overtime to comp-off instead of pay (Sec 42, to be used within 21 days)."
        actions={<Button variant="default" leftSection={<IconDownload size={16} stroke={1.6} />} disabled={!from || !to} onClick={() => from && to && otApi.registerCsv(from, to).catch((e) => toast.err(e))}>Export CSV</Button>} />
      <Group gap="xs" wrap="wrap" align="center">
        <DateField value={from} onChange={setFrom} w={170} placeholder="From" {...(to ? { maxDate: to } : {})} />
        <Text size="sm" c="dimmed">to</Text>
        <DateField value={to} onChange={setTo} w={170} placeholder="To" {...(from ? { minDate: from } : {})} />
        <Select value={status} onChange={setStatus} data={STATUS_OPTS} clearable placeholder="Any status" w={180} aria-label="Filter by status" />
        <Text size="sm" c="dimmed" ml="auto">{slips.length} {slips.length === 1 ? 'slip' : 'slips'}</Text>
      </Group>
      <DataTable<OtSlip> rows={slips} rowKey={(s) => s.id} loading={q.loading} error={q.error}
        emptyTitle={status === 'detected' ? 'No overtime waiting for review' : 'No overtime in this range'}
        emptyHint={status === 'detected' ? 'Overtime detected from punches shows up here for approval.' : 'Try a wider date range or another status.'}
        columns={[
          { key: 'emp', header: 'Employee', render: (s) => <><Text size="sm" fw={500}>{s.name_en}</Text><Text size="xs" c="dimmed">{s.emp_code}</Text></> },
          { key: 'date', header: 'Date', render: (s) => <><Text size="sm" style={{ whiteSpace: 'nowrap' }}>{fmtDateLong(s.local_date)}</Text><Text size="xs" c="dimmed">{dayType(s.day_type)}</Text></> },
          { key: 'det', header: 'Detected', align: 'right', render: (s) => <span className="num">{minutesToHm(s.detected_min)}</span> },
          { key: 'apr', header: 'Approved', align: 'right', render: (s) => <span className="num">{s.approved_min === null ? '—' : minutesToHm(s.approved_min)}</span> },
          { key: 'status', header: 'Status', render: (s) => <StatusBadge status={s.status} /> },
          { key: 'a', header: '', align: 'right', render: (s) => s.status === 'detected' || s.status === 'approved' ? (
            <Group gap={4} justify="flex-end" wrap="nowrap">
              {s.status === 'detected' ? <Button size="xs" variant="light" leftSection={<IconCheck size={14} stroke={1.8} />} onClick={() => { setApprove(s); setApprovedMin(s.detected_min); }}>Approve</Button> : null}
              <Button size="xs" variant="default" loading={busy === s.id} onClick={async () => { setBusy(s.id); try { const r = await otApi.compOff(s.id); toast.ok(`Credited ${r.days} ${Number(r.days) === 1 ? 'day' : 'days'} of comp-off, expires ${fmtAd(r.expiresOn)}.`); await q.reload(); } catch (e) { toast.err(e); } finally { setBusy(null); } }}>Give comp-off</Button>
              <Button size="xs" variant="subtle" color="red" onClick={() => setReject(s)}>Reject</Button>
            </Group>) : null },
        ]} />

      <Modal opened={!!approve} onClose={() => setApprove(null)} title="Approve overtime" centered>
        <Stack>
          {approve ? <Text size="sm" c="dimmed">{approve.name_en}, {fmtDateLong(approve.local_date)}. Detected {minutesToHm(approve.detected_min)}.</Text> : null}
          <NumberInput label="Approved minutes" value={approvedMin} onChange={setApprovedMin} min={0} max={Math.min(240, approve?.detected_min ?? 240)} step={15} description="Cannot exceed the detected minutes or the 4 h daily cap." />
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setApprove(null)}>Cancel</Button>
            <Button leftSection={<IconCheck size={16} stroke={1.6} />} onClick={async () => { if (!approve) return; try { await otApi.approveSlip(approve.id, Number(approvedMin)); toast.ok('Overtime approved.'); setApprove(null); await q.reload(); } catch (e) { toast.err(e); } }}>Approve {minutesToHm(Number(approvedMin) || 0)}</Button>
          </Group>
        </Stack>
      </Modal>
      <Modal opened={!!reject} onClose={() => setReject(null)} title="Reject overtime" centered>
        <Stack>
          {reject ? <Text size="sm" c="dimmed">{reject.name_en}, {fmtDateLong(reject.local_date)}. The employee will see your reason.</Text> : null}
          <Textarea label="Reason" description="At least 3 characters." required value={reason} onChange={(e) => setReason(e.currentTarget.value)} autosize minRows={2} data-autofocus />
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setReject(null)}>Cancel</Button>
            <Button color="red" leftSection={<IconX size={16} stroke={1.6} />} disabled={reason.trim().length < 3} onClick={async () => { if (!reject) return; try { await otApi.rejectSlip(reject.id, reason); toast.ok('Overtime rejected.'); setReject(null); setReason(''); await q.reload(); } catch (e) { toast.err(e); } }}>Reject overtime</Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
