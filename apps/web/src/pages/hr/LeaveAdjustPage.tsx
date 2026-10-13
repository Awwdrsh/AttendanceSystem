import { Button, Group, NumberInput, Paper, Select, SimpleGrid, Stack, Text, Textarea } from '@mantine/core';
import { useState } from 'react';
import { leave as leaveApi } from '../../api';
import { BsDatePicker } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { EmployeePicker } from '../../components/EmployeePicker';
import { PageHeader } from '../../components/PageHeader';
import { useI18n } from '../../i18n';
import { todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

export default function LeaveAdjustPage() {
  const { t } = useI18n();
  const [employeeId, setEmployeeId] = useState<string | null>(null);
  const types = useQuery(() => leaveApi.types(), []);
  const balances = useQuery(() => leaveApi.balances(employeeId!), [employeeId], { enabled: !!employeeId });
  const ledger = useQuery(() => leaveApi.ledger(employeeId!), [employeeId], { enabled: !!employeeId });
  const [typeId, setTypeId] = useState<string | null>(null); const [delta, setDelta] = useState<number | string>(1); const [kind, setKind] = useState('adjustment'); const [reason, setReason] = useState(''); const [date, setDate] = useState<string | null>(todayNpt());
  const [busy, setBusy] = useState(false);
  return (
    <Stack>
      <PageHeader title={t('leaveAdjust')} subtitle="Every adjustment is a signed ledger row; balances are never edited directly." />
      <SimpleGrid cols={{ base: 1, md: 2 }}>
        <Paper withBorder p="md" radius="md">
          <Stack>
            <EmployeePicker value={employeeId} onChange={setEmployeeId} required />
            <Select label={t('leave')} value={typeId} onChange={setTypeId} data={(types.data?.types ?? []).map((lt) => ({ value: lt.id, label: `${lt.code} · ${lt.name_en}` }))} required />
            <Group grow><NumberInput label="Days (+ credit / − debit)" value={delta} onChange={setDelta} decimalScale={2} /><Select label="Kind" value={kind} onChange={(v) => v && setKind(v)} allowDeselect={false} data={['adjustment', 'allocation', 'carry_forward']} /></Group>
            <BsDatePicker label="Effective date" value={date} onChange={setDate} />
            <Textarea label={t('reason')} required value={reason} onChange={(e) => setReason(e.currentTarget.value)} autosize minRows={2} />
            <Group justify="flex-end"><Button loading={busy} disabled={!employeeId || !typeId || !Number(delta) || reason.trim().length < 3} onClick={async () => { setBusy(true); try { const r = await leaveApi.adjust({ employeeId, leaveTypeId: typeId, deltaDays: Number(delta), reason, kind, ...(date ? { effectiveDate: date } : {}) }); toast.ok(`Posted; new balance ${r.balance.toFixed(2)} days.`); setReason(''); await Promise.all([balances.reload(), ledger.reload()]); } catch (e) { toast.err(e); } finally { setBusy(false); } }}>Post</Button></Group>
          </Stack>
        </Paper>
        <Paper withBorder p="md" radius="md">
          <Text fw={600} mb="xs">Balances</Text>
          {(balances.data?.balances ?? []).map((b) => <Text key={b.leaveTypeId} size="sm">{b.code}: <strong>{b.balance.toFixed(2)}</strong> ({b.available.toFixed(2)} available)</Text>)}
          {!employeeId ? <Text size="sm" c="dimmed">Pick an employee to see balances and the ledger.</Text> : null}
        </Paper>
      </SimpleGrid>
      {employeeId ? <DataTable rows={(ledger.data?.ledger ?? []) as Array<Record<string, unknown>>} rowKey={(r) => String(r.id)} loading={ledger.loading} emptyTitle="No ledger rows yet."
        columns={[{ key: 'effective_date', header: t('date'), render: (r) => String(r.effective_date) }, { key: 'code', header: t('leave'), render: (r) => String(r.code) }, { key: 'delta_days', header: 'Δ days', render: (r) => String(r.delta_days), align: 'right' }, { key: 'reason', header: 'Reason', render: (r) => String(r.reason) }, { key: 'note', header: 'Note', render: (r) => String(r.note ?? '') }]} /> : null}
    </Stack>
  );
}
