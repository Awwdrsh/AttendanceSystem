import { Alert, Button, Drawer, Group, Modal, Select, SimpleGrid, Stack, Switch, Text, Textarea, TextInput, Timeline } from '@mantine/core';
import { IconPlus } from '@tabler/icons-react';
import { useEffect, useMemo, useState } from 'react';
import { leave as leaveApi, shifts as shiftApi } from '../../api';
import { ApiError } from '../../api/client';
import type { LeaveRequest, LeavePreview } from '../../api/types';
import { useAuth } from '../../auth/AuthProvider';
import { DateField } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { Field, StatStrip } from '../../components/Kpi';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { StatusBadge, statusLabel } from '../../components/StatusBadge';
import { fmtAd, fmtDateShort, fmtTime, NPT_OFFSET_MIN, todayNpt, addDays } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { ApprovalChainPreview } from '../../components/ApprovalChainPreview';

const fmtDays = (n: number) => `${Number.isInteger(n) ? n : n.toFixed(1)} ${n === 1 ? 'day' : 'days'}`;

/** "13 Oct 2026, 10:17" in Nepal time. */
function fmtStamp(iso: string | null | undefined): string {
  if (!iso) return '—';
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return '—';
  return `${fmtAd(new Date(ms + NPT_OFFSET_MIN * 60_000).toISOString())}, ${fmtTime(iso)}`;
}

function dateRange(from: string, to: string): string {
  return from === to ? fmtAd(from) : `${fmtDateShort(from)} to ${fmtAd(to)}`;
}

/** Reusable request form (employee self-service and HR/manager on behalf). */
export function LeaveApplyForm({ employeeId, onDone }: { employeeId: string; onDone: () => void }) {
  const types = useQuery(() => leaveApi.types(), []);
  const balances = useQuery(() => leaveApi.balances(employeeId), [employeeId]);
  const [typeId, setTypeId] = useState<string | null>(null);
  const [from, setFrom] = useState<string | null>(todayNpt());
  const [to, setTo] = useState<string | null>(todayNpt());
  const [half, setHalf] = useState(false); const [halfWhich, setHalfWhich] = useState<'first_half' | 'second_half'>('first_half');
  const [hourly, setHourly] = useState(false); const [fromTime, setFromTime] = useState('10:00'); const [toTime, setToTime] = useState('12:00');
  const selectedType = (types.data?.types ?? []).find((lt) => lt.id === typeId);
  const canHourly = !!selectedType?.allow_hourly && !!from && from === to;
  const byHours = hourly && canHourly;
  const hourWindow = byHours ? { fromTime, toTime } : {};
  const [reason, setReason] = useState(''); const [attachment, setAttachment] = useState('');
  const [preview, setPreview] = useState<LeavePreview | null>(null);
  const [suggestion, setSuggestion] = useState<{ msg: string; paidDays: number; lwpDays: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const cal = useQuery(() => shiftApi.calendar(addDays(todayNpt(), -60), addDays(todayNpt(), 120), employeeId), [employeeId]);
  const holidays = useMemo(() => new Map((cal.data?.days ?? []).filter((d) => d.holidays.length).map((d) => [d.date, d.holidays.map((h) => h.name_en).join(', ')])), [cal.data]);

  useEffect(() => {
    if (!typeId || !from || !to || to < from) { setPreview(null); return; }
    if (byHours && !(fromTime < toTime)) { setPreview(null); return; }
    leaveApi.preview({ employeeId, leaveTypeId: typeId, from, to, ...(half && !byHours ? { halfDay: halfWhich } : {}), ...hourWindow }).then(setPreview).catch(() => setPreview(null));
  }, [typeId, from, to, half, halfWhich, employeeId, byHours, fromTime, toTime]); // eslint-disable-line react-hooks/exhaustive-deps

  const bal = (id: string) => balances.data?.balances.find((b) => b.leaveTypeId === id);
  const submit = async () => {
    if (!typeId || !from || !to) return;
    setBusy(true); setSuggestion(null);
    try {
      const r = await leaveApi.request({ employeeId, leaveTypeId: typeId, from, to, ...(half && !byHours ? { halfDay: halfWhich } : {}), ...hourWindow, ...(reason ? { reason } : {}), ...(attachment ? { attachmentKey: attachment } : {}) });
      toast.ok(`${r.explanation}${r.teamOverlap.length ? ` ${r.teamOverlap.join(', ')} are also off; your manager will see this.` : ''}`, 'Time off requested');
      onDone();
    } catch (e) {
      if (e instanceof ApiError && e.suggestion && typeof e.suggestion === 'object') { const s = e.suggestion as { paidDays: number; lwpDays: number }; setSuggestion({ msg: e.message, paidDays: s.paidDays, lwpDays: s.lwpDays }); }
      else toast.err(e, 'Request not sent');
    } finally { setBusy(false); }
  };

  return (
    <Stack>
      <Select label="Type" required value={typeId} onChange={setTypeId} placeholder="Choose a type of leave"
        data={(types.data?.types ?? []).map((lt) => { const b = bal(lt.id); return { value: lt.id, label: `${lt.name_en}${b ? ` (${b.available.toFixed(1)} days left)` : ''}` }; })} />
      <SimpleGrid cols={{ base: 1, sm: 2 }}>
        <DateField label="From" value={from} onChange={(d) => { setFrom(d); if (d && (!to || to < d)) setTo(d); }} holidays={holidays} required />
        <DateField label="To" value={to} onChange={setTo} holidays={holidays} {...(from ? { minDate: from } : {})} required />
      </SimpleGrid>
      <Group gap="md" wrap="wrap">
        <Switch label="Half day" {...(!!from && !!to && from !== to ? { description: 'Only for a single day' } : {})} checked={half && !byHours} onChange={(e) => { setHalf(e.currentTarget.checked); if (e.currentTarget.checked) setHourly(false); }} disabled={!!from && !!to && from !== to} />
        {half && !byHours ? <Select aria-label="Which half" value={halfWhich} onChange={(v) => v && setHalfWhich(v as typeof halfWhich)} allowDeselect={false} data={[{ value: 'first_half', label: 'First half' }, { value: 'second_half', label: 'Second half' }]} w={160} /> : null}
        {selectedType?.allow_hourly ? (
          <Switch label="By the hour" {...(!canHourly ? { description: 'Only for a single day' } : { description: 'Leave for part of the day, e.g. 10:00 to 12:00' })}
            checked={byHours} disabled={!canHourly} onChange={(e) => { setHourly(e.currentTarget.checked); if (e.currentTarget.checked) setHalf(false); }} />
        ) : null}
      </Group>
      {byHours ? (
        <SimpleGrid cols={2}>
          <TextInput type="time" label="From" value={fromTime} onChange={(e) => setFromTime(e.currentTarget.value)} required step={900} />
          <TextInput type="time" label="To" value={toTime} onChange={(e) => setToTime(e.currentTarget.value)} required step={900}
            {...(fromTime && toTime && toTime <= fromTime ? { error: 'Must be after the start time' } : {})} />
        </SimpleGrid>
      ) : null}
      <ApprovalChainPreview objectType="leave_request" employeeId={employeeId} />
      {preview ? (
        <Alert variant="light" color={preview.balanceAfter < 0 ? 'orange' : 'gray'}>
          <Text size="sm" fw={600}>{preview.hours ? `${preview.hours} ${preview.hours === 1 ? 'hour' : 'hours'} (${preview.days} of a day)` : fmtDays(preview.days)} will be deducted</Text>
          <Text size="sm" c="dimmed">{preview.explanation}{preview.sandwichApplied ? ' Weekends or holidays between leave days are counted (sandwich rule).' : ''}</Text>
          <Text size="sm" mt={4}>Balance after this request: <Text span fw={600} className="num">{preview.balanceAfter.toFixed(1)}</Text> of {preview.available.toFixed(1)} days</Text>
        </Alert>
      ) : null}
      {suggestion ? (
        <Alert variant="light" color="orange" title="Not enough balance">
          <Text size="sm">{suggestion.msg}</Text>
          <Text size="sm" c="dimmed" mt={4}>You can take {suggestion.paidDays} paid and {suggestion.lwpDays} unpaid. Change the dates, or request the unpaid part as leave without pay.</Text>
        </Alert>
      ) : null}
      <Textarea label="Reason" description="Optional. Your manager sees this." value={reason} onChange={(e) => setReason(e.currentTarget.value)} autosize minRows={2} />
      <TextInput label="Attachment reference" description="Optional. For example a medical certificate number; required for sick leave over 3 days." value={attachment} onChange={(e) => setAttachment(e.currentTarget.value)} />
      <Group justify="flex-end"><Button loading={busy} disabled={!typeId || !from || !to} onClick={submit}>Send request</Button></Group>
    </Stack>
  );
}

export default function LeavePage() {
  const { user } = useAuth();
  const employeeId = user?.employeeId ?? '';
  const balances = useQuery(() => leaveApi.balances(employeeId), [employeeId], { enabled: !!employeeId });
  const requests = useQuery(() => leaveApi.requests({ scope: 'own' }), [employeeId], { enabled: !!employeeId });
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<LeaveRequest | null>(null);

  const cancel = async (r: LeaveRequest) => { try { await leaveApi.cancel(r.id); toast.ok('Request cancelled.'); await Promise.all([requests.reload(), balances.reload()]); } catch (e) { toast.err(e, 'Could not cancel'); } };
  const shown = (balances.data?.balances ?? []).filter((b) => b.paid || b.balance !== 0);

  return (
    <Stack gap="lg">
      <PageHeader title="Time off" subtitle="Your balances for this fiscal year and every request you've made."
        actions={<Button leftSection={<IconPlus size={16} stroke={1.6} />} onClick={() => setOpen(true)} disabled={!employeeId}>Request time off</Button>} />

      {shown.length ? (
        <StatStrip stats={shown.map((b) => ({
          key: b.leaveTypeId, label: b.nameEn, value: b.available.toFixed(1),
          hint: b.pending > 0 ? `days available, ${b.pending} pending` : 'days available',
        }))} />
      ) : null}

      <div>
        <SectionTitle>Requests</SectionTitle>
        <DataTable<LeaveRequest> rows={requests.data?.requests ?? []} rowKey={(r) => r.id} loading={requests.loading} error={requests.error}
          emptyTitle="No time off requested yet" emptyHint="Use Request time off to ask for leave. Your manager is notified straight away."
          onRowClick={setDetail}
          columns={[
            { key: 'type', header: 'Type', render: (r) => <Text size="sm" fw={500}>{r.type_name}</Text> },
            { key: 'dates', header: 'Dates', render: (r) => <>{dateRange(r.from_date, r.to_date)}{r.half_day ? <Text span size="sm" c="dimmed">{` (${r.half_day === 'first_half' ? 'first half' : 'second half'})`}</Text> : null}{r.from_time ? <Text span size="sm" c="dimmed" className="num">{` ${r.from_time}–${r.to_time}`}</Text> : null}</> },
            { key: 'days', header: 'Days', render: (r) => <span className="num">{Number(r.days_requested)}{r.hours ? <Text span size="xs" c="dimmed">{` (${Number(r.hours)} h)`}</Text> : null}</span>, align: 'right' },
            { key: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} /> },
            { key: 'actions', header: '', align: 'right', render: (r) => (r.status === 'pending' || (r.status === 'approved' && r.from_date > todayNpt())) ? <Button size="xs" variant="subtle" color="red" onClick={(e) => { e.stopPropagation(); void cancel(r); }}>Cancel request</Button> : null },
          ]} />
      </div>

      <Drawer opened={open} onClose={() => setOpen(false)} position="right" size="md" title={<Text fw={600}>Request time off</Text>}>
        {employeeId ? <LeaveApplyForm employeeId={employeeId} onDone={() => { setOpen(false); void requests.reload(); void balances.reload(); }} /> : null}
      </Drawer>

      <Modal opened={!!detail} onClose={() => setDetail(null)} title={<Text fw={600}>{detail?.type_name}</Text>} centered>
        {detail ? (
          <Stack>
            <SimpleGrid cols={2}>
              <Field label="Dates">{dateRange(detail.from_date, detail.to_date)}</Field>
              <Field label="Days">{Number(detail.days_requested)}{detail.half_day ? ` (${detail.half_day === 'first_half' ? 'first half' : 'second half'})` : ''}{detail.from_time ? ` (${detail.from_time}–${detail.to_time}, ${Number(detail.hours)} h)` : ''}</Field>
            </SimpleGrid>
            {detail.reason ? <Field label="Reason">{detail.reason}</Field> : null}
            <Timeline active={detail.status === 'pending' ? 0 : 1} bulletSize={14} lineWidth={1}>
              <Timeline.Item title="Submitted"><Text size="xs" c="dimmed">{fmtStamp(detail.created_at)}</Text></Timeline.Item>
              <Timeline.Item title={detail.status === 'pending' ? 'Waiting for your manager' : statusLabel(detail.status)}><Text size="xs" c="dimmed">{detail.decided_at ? fmtStamp(detail.decided_at) : 'Not decided yet'}</Text></Timeline.Item>
              <Timeline.Item title="Balance updated" />
            </Timeline>
          </Stack>
        ) : null}
      </Modal>
    </Stack>
  );
}
