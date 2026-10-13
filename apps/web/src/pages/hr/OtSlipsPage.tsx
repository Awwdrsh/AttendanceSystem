import { Button, Group, Modal, NumberInput, Select, Stack, Text, Textarea } from '@mantine/core';
import { useState } from 'react';
import { ot as otApi } from '../../api';
import type { OtSlip } from '../../api/types';
import { BsDatePicker } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { useI18n } from '../../i18n';
import { addDays, dualDate, minutesToHm, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

export default function OtSlipsPage() {
  const { t, lang, numerals } = useI18n();
  const [from, setFrom] = useState<string | null>(addDays(todayNpt(), -30)); const [to, setTo] = useState<string | null>(todayNpt()); const [status, setStatus] = useState<string | null>('detected');
  const q = useQuery(() => otApi.slips({ from, to, status }), [from, to, status]);
  const [approve, setApprove] = useState<OtSlip | null>(null); const [approvedMin, setApprovedMin] = useState<number | string>(0);
  const [reject, setReject] = useState<OtSlip | null>(null); const [reason, setReason] = useState('');
  return (
    <Stack>
      <PageHeader title={t('otSlips')} subtitle={lang === 'ne' ? 'स्वीकृत घण्टा मात्र तलबमा जान्छ; वा सट्टा बिदामा बदल्नुहोस् (दफा ४२, २१ दिनभित्र)।' : 'Only approved hours reach payroll. Convert to comp-off instead of pay where policy allows (Sec 42, use within 21 days).'}
        actions={<Group gap="xs"><BsDatePicker value={from} onChange={setFrom} w={220} /><BsDatePicker value={to} onChange={setTo} w={220} /><Select value={status} onChange={setStatus} data={['detected', 'approved', 'rejected', 'posted', 'comp_off']} clearable placeholder="Any" w={140} /><Button variant="light" onClick={() => from && to && otApi.registerCsv(from, to).catch((e) => toast.err(e))}>{t('csv')}</Button></Group>} />
      <DataTable<OtSlip> rows={q.data?.slips ?? []} rowKey={(s) => s.id} loading={q.loading} error={q.error} emptyTitle="No overtime slips in this range."
        columns={[
          { key: 'emp', header: t('name'), render: (s) => <><Text size="sm" fw={500}>{s.name_en}</Text><Text size="xs" c="dimmed">{s.emp_code}</Text></> },
          { key: 'date', header: t('date'), render: (s) => `${dualDate(s.local_date, lang, numerals).bs} (${s.day_type})` },
          { key: 'det', header: 'Detected', render: (s) => minutesToHm(s.detected_min, numerals) },
          { key: 'apr', header: 'Approved', render: (s) => s.approved_min === null ? '—' : minutesToHm(s.approved_min, numerals) },
          { key: 'status', header: t('status'), render: (s) => <StatusBadge status={s.status} /> },
          { key: 'a', header: '', render: (s) => s.status === 'detected' || s.status === 'approved' ? <Group gap={4}>
            {s.status === 'detected' ? <Button size="xs" onClick={() => { setApprove(s); setApprovedMin(s.detected_min); }}>{t('approve')}</Button> : null}
            <Button size="xs" variant="light" onClick={async () => { try { const r = await otApi.compOff(s.id); toast.ok(`Credited ${r.days} day(s) substitute leave, expires ${r.expiresOn}.`); await q.reload(); } catch (e) { toast.err(e); } }}>Comp-off</Button>
            <Button size="xs" variant="subtle" color="red" onClick={() => setReject(s)}>{t('reject')}</Button></Group> : null },
        ]} />
      <Modal opened={!!approve} onClose={() => setApprove(null)} title={`${t('approve')} · ${approve?.name_en ?? ''}`} centered>
        <Stack><NumberInput label="Approved minutes" value={approvedMin} onChange={setApprovedMin} min={0} max={Math.min(240, approve?.detected_min ?? 240)} step={15} description="Cannot exceed detected minutes or the 4 h daily cap." />
          <Group justify="flex-end"><Button onClick={async () => { if (!approve) return; try { await otApi.approveSlip(approve.id, Number(approvedMin)); toast.ok('Approved.'); setApprove(null); await q.reload(); } catch (e) { toast.err(e); } }}>{t('approve')}</Button></Group></Stack>
      </Modal>
      <Modal opened={!!reject} onClose={() => setReject(null)} title={t('reject')} centered>
        <Stack><Textarea label={t('reason')} required value={reason} onChange={(e) => setReason(e.currentTarget.value)} autosize minRows={2} />
          <Group justify="flex-end"><Button color="red" disabled={reason.trim().length < 3} onClick={async () => { if (!reject) return; try { await otApi.rejectSlip(reject.id, reason); setReject(null); setReason(''); await q.reload(); } catch (e) { toast.err(e); } }}>{t('reject')}</Button></Group></Stack>
      </Modal>
    </Stack>
  );
}
