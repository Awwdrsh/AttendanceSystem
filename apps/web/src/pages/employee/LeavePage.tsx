import { Alert, Button, Group, Modal, Paper, Select, SimpleGrid, Stack, Switch, Text, Textarea, TextInput, Timeline } from '@mantine/core';
import { useEffect, useMemo, useState } from 'react';
import { leave as leaveApi, shifts as shiftApi } from '../../api';
import { ApiError } from '../../api/client';
import type { LeaveRequest, LeavePreview } from '../../api/types';
import { useAuth } from '../../auth/AuthProvider';
import { BsDatePicker } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { useI18n } from '../../i18n';
import { dualDate, fmtNum, todayNpt, addDays } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

/** Reusable apply form (employee self-service and HR/manager on behalf). */
export function LeaveApplyForm({ employeeId, onDone }: { employeeId: string; onDone: () => void }) {
  const { t, lang } = useI18n();
  const types = useQuery(() => leaveApi.types(), []);
  const balances = useQuery(() => leaveApi.balances(employeeId), [employeeId]);
  const [typeId, setTypeId] = useState<string | null>(null);
  const [from, setFrom] = useState<string | null>(todayNpt());
  const [to, setTo] = useState<string | null>(todayNpt());
  const [half, setHalf] = useState(false); const [halfWhich, setHalfWhich] = useState<'first_half' | 'second_half'>('first_half');
  const [reason, setReason] = useState(''); const [attachment, setAttachment] = useState('');
  const [preview, setPreview] = useState<LeavePreview | null>(null);
  const [suggestion, setSuggestion] = useState<{ msg: string; paidDays: number; lwpDays: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const cal = useQuery(() => shiftApi.calendar(addDays(todayNpt(), -60), addDays(todayNpt(), 120), employeeId), [employeeId]);
  const holidays = useMemo(() => new Map((cal.data?.days ?? []).filter((d) => d.holidays.length).map((d) => [d.date, d.holidays.map((h) => h.name_en).join(', ')])), [cal.data]);

  useEffect(() => {
    if (!typeId || !from || !to || to < from) { setPreview(null); return; }
    leaveApi.preview({ employeeId, leaveTypeId: typeId, from, to, ...(half ? { halfDay: halfWhich } : {}) }).then(setPreview).catch(() => setPreview(null));
  }, [typeId, from, to, half, halfWhich, employeeId]);

  const bal = (id: string) => balances.data?.balances.find((b) => b.leaveTypeId === id);
  const submit = async () => {
    if (!typeId || !from || !to) return;
    setBusy(true); setSuggestion(null);
    try {
      const r = await leaveApi.request({ employeeId, leaveTypeId: typeId, from, to, ...(half ? { halfDay: halfWhich } : {}), ...(reason ? { reason } : {}), ...(attachment ? { attachmentKey: attachment } : {}) });
      toast.ok(`${r.explanation}${r.teamOverlap.length ? ` ${r.teamOverlap.join(', ')} ${lang === 'ne' ? 'पनि बिदामा छन्' : 'are also off; your manager will see this.'}` : ''}`, lang === 'ne' ? 'बिदा अनुरोध पेश भयो' : 'Leave request submitted');
      onDone();
    } catch (e) {
      if (e instanceof ApiError && e.suggestion && typeof e.suggestion === 'object') { const s = e.suggestion as { paidDays: number; lwpDays: number }; setSuggestion({ msg: e.message, paidDays: s.paidDays, lwpDays: s.lwpDays }); }
      else toast.err(e);
    } finally { setBusy(false); }
  };

  return (
    <Stack>
      <Select label={t('leave')} required value={typeId} onChange={setTypeId} placeholder="Choose a leave type" data={(types.data?.types ?? []).map((lt) => { const b = bal(lt.id); return { value: lt.id, label: `${lang === 'ne' && lt.name_ne ? lt.name_ne : lt.name_en}${b ? ` · ${b.available.toFixed(1)} ${t('days')}` : ''}` }; })} />
      <Group grow align="flex-start">
        <BsDatePicker label={t('from')} value={from} onChange={(d) => { setFrom(d); if (d && (!to || to < d)) setTo(d); }} holidays={holidays} required />
        <BsDatePicker label={t('to')} value={to} onChange={setTo} holidays={holidays} {...(from ? { minDate: from } : {})} required />
      </Group>
      <Group>
        <Switch label={t('halfDay')} checked={half} onChange={(e) => setHalf(e.currentTarget.checked)} disabled={!!from && !!to && from !== to} />
        {half ? <Select value={halfWhich} onChange={(v) => v && setHalfWhich(v as typeof halfWhich)} allowDeselect={false} data={[{ value: 'first_half', label: t('firstHalf') }, { value: 'second_half', label: t('secondHalf') }]} w={160} /> : null}
      </Group>
      {preview ? <Alert variant="light" color={preview.balanceAfter < 0 ? 'orange' : 'teal'}>
        <Text size="sm" fw={600}>{fmtNum(preview.days)} {lang === 'ne' ? 'कार्य दिन कट्टा हुनेछ' : 'working day(s) will be deducted'}</Text>
        <Text size="xs">{preview.explanation}{preview.sandwichApplied ? ' (sandwich rule applied)' : ''} · {t('balance')} {preview.available.toFixed(1)} → {preview.balanceAfter.toFixed(1)}</Text>
      </Alert> : null}
      {suggestion ? <Alert variant="light" color="orange" title={lang === 'ne' ? 'बाँकी बिदा अपुग' : 'Not enough balance'}>
        <Text size="sm">{suggestion.msg}</Text>
        <Text size="xs" c="dimmed" mt={4}>{lang === 'ne' ? 'सुझाव' : 'Suggestion'}: {suggestion.paidDays} paid + {suggestion.lwpDays} unpaid. Change the dates, or apply the unpaid part as LWP.</Text>
      </Alert> : null}
      <Textarea label={t('reason')} value={reason} onChange={(e) => setReason(e.currentTarget.value)} autosize minRows={2} />
      <TextInput label={lang === 'ne' ? 'कागजात (वैकल्पिक)' : 'Attachment reference (optional)'} description="e.g. medical certificate number; required for sick leave over 3 days" value={attachment} onChange={(e) => setAttachment(e.currentTarget.value)} />
      <Group justify="flex-end"><Button h={48} loading={busy} disabled={!typeId || !from || !to} onClick={submit}>{t('apply')}</Button></Group>
    </Stack>
  );
}

export default function LeavePage() {
  const { user } = useAuth();
  const { t, lang, numerals } = useI18n();
  const employeeId = user?.employeeId ?? '';
  const balances = useQuery(() => leaveApi.balances(employeeId), [employeeId], { enabled: !!employeeId });
  const requests = useQuery(() => leaveApi.requests({ scope: 'own' }), [employeeId], { enabled: !!employeeId });
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<LeaveRequest | null>(null);

  const cancel = async (r: LeaveRequest) => { try { await leaveApi.cancel(r.id); toast.ok('Request cancelled.'); await Promise.all([requests.reload(), balances.reload()]); } catch (e) { toast.err(e); } };

  return (
    <Stack>
      <PageHeader title={t('leave')} actions={<Button h={48} onClick={() => setOpen(true)}>{t('apply')}</Button>} />
      <SimpleGrid cols={{ base: 1, sm: 2, lg: 3 }} spacing="sm">
        {(balances.data?.balances ?? []).filter((b) => b.paid || b.balance !== 0).map((b) => (
          <Paper key={b.leaveTypeId} withBorder p="md" radius="md">
            <Text size="xs" c="dimmed" tt="uppercase" fw={600}>{lang === 'ne' && b.nameNe ? b.nameNe : b.nameEn}</Text>
            <Text fz={26} fw={700}>{fmtNum(b.available.toFixed(1), numerals)} <Text span size="sm" c="dimmed">{t('days')}</Text></Text>
            <Text size="xs" c="dimmed">{b.explanation}</Text>
          </Paper>
        ))}
      </SimpleGrid>
      <DataTable<LeaveRequest> rows={requests.data?.requests ?? []} rowKey={(r) => r.id} loading={requests.loading} error={requests.error} emptyTitle={lang === 'ne' ? 'अहिलेसम्म कुनै बिदा अनुरोध छैन।' : 'No leave requests yet.'} emptyHint={lang === 'ne' ? 'माथिको "आवेदन" बटनबाट सुरु गर्नुहोस्।' : 'Use the Apply button to request leave.'}
        onRowClick={setDetail}
        columns={[
          { key: 'type', header: t('leave'), render: (r) => r.type_name },
          { key: 'dates', header: t('date'), render: (r) => `${dualDate(r.from_date, lang, numerals).bs}${r.to_date !== r.from_date ? ` → ${dualDate(r.to_date, lang, numerals).bs}` : ''}${r.half_day ? ` (${r.half_day === 'first_half' ? t('firstHalf') : t('secondHalf')})` : ''}` },
          { key: 'days', header: t('days'), render: (r) => fmtNum(Number(r.days_requested), numerals), align: 'right' },
          { key: 'status', header: t('status'), render: (r) => <StatusBadge status={r.status} /> },
          { key: 'actions', header: '', render: (r) => (r.status === 'pending' || (r.status === 'approved' && r.from_date > todayNpt())) ? <Button size="xs" variant="subtle" color="red" onClick={(e) => { e.stopPropagation(); void cancel(r); }}>{t('cancel')}</Button> : null },
        ]} />
      <Modal opened={open} onClose={() => setOpen(false)} title={`${t('apply')} · ${t('leave')}`} size="lg" centered>
        {employeeId ? <LeaveApplyForm employeeId={employeeId} onDone={() => { setOpen(false); void requests.reload(); void balances.reload(); }} /> : null}
      </Modal>
      <Modal opened={!!detail} onClose={() => setDetail(null)} title={detail?.type_name} centered>
        {detail ? <Stack gap="xs">
          <Text size="sm">{dualDate(detail.from_date, lang, numerals).bsLong} → {dualDate(detail.to_date, lang, numerals).bsLong} · {detail.days_requested} {t('days')}</Text>
          {detail.reason ? <Text size="sm" c="dimmed">{detail.reason}</Text> : null}
          <Timeline active={detail.status === 'pending' ? 0 : 1} bulletSize={18} lineWidth={2}>
            <Timeline.Item title={lang === 'ne' ? 'पेश गरियो' : 'Submitted'}><Text size="xs" c="dimmed">{new Date(detail.created_at).toLocaleString()}</Text></Timeline.Item>
            <Timeline.Item title={detail.status === 'pending' ? (lang === 'ne' ? 'प्रबन्धकसँग विचाराधीन' : 'Pending with manager') : detail.status}><Text size="xs" c="dimmed">{detail.decided_at ? new Date(detail.decided_at).toLocaleString() : '—'}</Text></Timeline.Item>
            <Timeline.Item title={lang === 'ne' ? 'बाँकी अद्यावधिक' : 'Balance updated'} />
          </Timeline>
        </Stack> : null}
      </Modal>
    </Stack>
  );
}
