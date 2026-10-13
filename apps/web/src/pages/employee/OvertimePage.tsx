import { Button, Group, Modal, NumberInput, Stack, Text, Textarea } from '@mantine/core';
import { useState } from 'react';
import { ot as otApi } from '../../api';
import type { OtRequest, OtSlip } from '../../api/types';
import { useAuth } from '../../auth/AuthProvider';
import { BsDatePicker } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { useI18n } from '../../i18n';
import { dualDate, minutesToHm, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

export default function OvertimePage() {
  const { user } = useAuth();
  const { t, lang, numerals } = useI18n();
  const requests = useQuery(() => otApi.requests({ scope: 'own' }), []);
  const slips = useQuery(() => otApi.slips(), []);
  const [open, setOpen] = useState(false);
  const [date, setDate] = useState<string | null>(todayNpt()); const [minutes, setMinutes] = useState<number | string>(60); const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    if (!date) return; setBusy(true);
    try { await otApi.request({ localDate: date, requestedMin: Number(minutes), reason }); toast.ok('Overtime request sent to your manager.'); setOpen(false); await requests.reload(); }
    catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  if (!user?.employeeId) return null;
  return (
    <Stack>
      <PageHeader title={t('overtime')} subtitle={lang === 'ne' ? 'स्वीकृत ओभरटाइम मात्र तलबमा जान्छ। सीमा: दिनमा ४ घण्टा, हप्तामा २४ घण्टा।' : 'Only approved overtime is paid. Caps: 4 h per day, 24 h per week (Labour Act Sec 30).'} actions={<Button h={48} onClick={() => setOpen(true)}>{lang === 'ne' ? 'पूर्व-स्वीकृति माग्नुहोस्' : 'Request pre-approval'}</Button>} />
      <Text fw={600}>{lang === 'ne' ? 'मेरा अनुरोध' : 'My requests'}</Text>
      <DataTable<OtRequest> rows={requests.data?.requests ?? []} rowKey={(r) => r.id} loading={requests.loading} error={requests.error} emptyTitle="No overtime requests yet."
        columns={[
          { key: 'date', header: t('date'), render: (r) => dualDate(r.local_date, lang, numerals).bs },
          { key: 'req', header: 'Requested', render: (r) => minutesToHm(r.requested_min, numerals) },
          { key: 'apr', header: 'Approved', render: (r) => r.approved_min === null ? '—' : minutesToHm(r.approved_min, numerals) },
          { key: 'status', header: t('status'), render: (r) => <StatusBadge status={r.status} /> },
          { key: 'a', header: '', render: (r) => r.status === 'pending' ? <Button size="xs" variant="subtle" color="red" onClick={async () => { try { await otApi.cancelRequest(r.id); await requests.reload(); } catch (e) { toast.err(e); } }}>{t('cancel')}</Button> : null },
        ]} />
      <Text fw={600}>{lang === 'ne' ? 'पत्ता लागेको ओभरटाइम' : 'Detected overtime'}</Text>
      <DataTable<OtSlip> rows={slips.data?.slips ?? []} rowKey={(r) => r.id} loading={slips.loading} error={slips.error} emptyTitle="No overtime detected yet."
        columns={[
          { key: 'date', header: t('date'), render: (r) => dualDate(r.local_date, lang, numerals).bs },
          { key: 'det', header: 'Detected', render: (r) => minutesToHm(r.detected_min, numerals) },
          { key: 'apr', header: 'Approved', render: (r) => r.approved_min === null ? '—' : minutesToHm(r.approved_min, numerals) },
          { key: 'rate', header: 'Rate', render: (r) => `${r.multiplier}× (${r.day_type})` },
          { key: 'status', header: t('status'), render: (r) => <StatusBadge status={r.status} /> },
        ]} />
      <Modal opened={open} onClose={() => setOpen(false)} title="Overtime pre-approval" centered>
        <Stack>
          <BsDatePicker label={t('date')} value={date} onChange={setDate} required />
          <NumberInput label="Minutes" value={minutes} onChange={setMinutes} min={15} max={240} step={15} />
          <Textarea label={t('reason')} value={reason} onChange={(e) => setReason(e.currentTarget.value)} autosize minRows={2} />
          <Group justify="flex-end"><Button h={48} loading={busy} onClick={submit}>{t('submit')}</Button></Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
