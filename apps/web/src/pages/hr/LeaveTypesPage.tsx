import { Badge, Button, Group, Modal, NumberInput, Select, SimpleGrid, Stack, Switch, Text, TextInput } from '@mantine/core';
import { useState } from 'react';
import { leave as leaveApi } from '../../api';
import type { LeaveType } from '../../api/types';
import { DataTable } from '../../components/DataTable';
import { PageHeader } from '../../components/PageHeader';
import { useI18n } from '../../i18n';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const CITATION: Record<string, string> = { HOME: 'Labour Act 2074 Sec 43, 49: 1 day per 20 worked, cap 90', SICK: 'Sec 44: 12/yr, cap 45, certificate over 3 days', MATERNITY: 'Sec 45: 14 weeks, 60 paid days', PATERNITY: 'Sec 45(7): 15 days', MOURNING: 'Sec 48: 13 days', SUBSTITUTE: 'Sec 42: within 21 days', SPECIAL_UNPAID: 'Sec 80(3)' };

export default function LeaveTypesPage() {
  const { t } = useI18n();
  const q = useQuery(() => leaveApi.types(), []);
  const [edit, setEdit] = useState<Record<string, unknown> | null>(null); const [busy, setBusy] = useState(false);
  const locked = !!edit?.statutory;
  const save = async () => {
    if (!edit) return; setBusy(true);
    try {
      const { id, statutory: _s, ...b } = edit as { id?: string; statutory?: boolean } & Record<string, unknown>;
      const body: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(b)) if (v !== undefined && v !== '') body[k] = v;
      if (locked) for (const k of ['accrualBasis', 'accrualDays', 'accrualPerWorkedDays', 'maxBalance', 'paid', 'genderScope', 'expiryDays']) delete body[k];
      if (id) await leaveApi.updateType(id, body); else await leaveApi.createType(body);
      toast.ok('Leave type saved.'); setEdit(null); await q.reload();
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  const open = (lt?: LeaveType) => setEdit(lt ? { id: lt.id, statutory: lt.statutory, code: lt.code, nameEn: lt.name_en, nameNe: lt.name_ne ?? '', paid: lt.paid, accrualBasis: lt.accrual_basis, accrualDays: Number(lt.accrual_days), accrualPerWorkedDays: lt.accrual_per_worked_days ?? undefined, maxBalance: lt.max_balance === null ? undefined : Number(lt.max_balance), carryForward: lt.carry_forward, encashable: lt.encashable, certificateAfterDays: lt.certificate_after_days ?? undefined, allowHalfDay: lt.allow_half_day, maxConsecutiveDays: lt.max_consecutive_days ?? undefined, minServiceDays: lt.min_service_days, negativeBalanceLimit: Number(lt.negative_balance_limit), genderScope: lt.gender_scope, sandwichRule: lt.sandwich_rule, expiryDays: lt.expiry_days ?? undefined } : { code: '', nameEn: '', nameNe: '', paid: true, accrualBasis: 'annual', accrualDays: 0, carryForward: false, encashable: false, allowHalfDay: true, minServiceDays: 0, negativeBalanceLimit: 0, genderScope: 'all', sandwichRule: false, statutory: false });
  const set = (k: string, v: unknown) => setEdit((e) => e && ({ ...e, [k]: v }));
  const num = (k: string, label: string, extra: Record<string, unknown> = {}) => <NumberInput label={label} value={(edit?.[k] as number | undefined) ?? ''} onChange={(v) => set(k, v === '' ? undefined : Number(v))} min={0} {...extra} />;
  return (
    <Stack>
      <PageHeader title={t('leaveTypes')} subtitle="Statutory entitlements are locked with their citation; company types are fully editable (plan §13.8)." actions={<Button onClick={() => open()}>{t('create')}</Button>} />
      <DataTable<LeaveType> rows={q.data?.types ?? []} rowKey={(l) => l.id} loading={q.loading} error={q.error} emptyTitle="No leave types." onRowClick={(l) => open(l)}
        columns={[
          { key: 'code', header: t('code'), render: (l) => <Group gap={4}><Text size="sm" fw={500}>{l.code}</Text>{l.statutory ? <Badge size="xs" variant="light" color="gray">🔒 statutory</Badge> : null}</Group> },
          { key: 'name', header: t('name'), render: (l) => `${l.name_en}${l.name_ne ? ` / ${l.name_ne}` : ''}` },
          { key: 'paid', header: 'Paid', render: (l) => l.paid ? 'yes' : 'no' },
          { key: 'accrual', header: 'Accrual', render: (l) => l.accrual_basis === 'worked_days' ? `${l.accrual_days} per ${l.accrual_per_worked_days} worked days` : l.accrual_basis === 'monthly' ? `${l.accrual_days}/month` : l.accrual_basis === 'annual' ? `${l.accrual_days}/year` : '—' },
          { key: 'cap', header: 'Cap', render: (l) => l.max_balance ?? '—' },
          { key: 'cite', header: 'Basis', render: (l) => <Text size="xs" c="dimmed">{CITATION[l.code] ?? 'Company policy'}</Text> },
        ]} />
      <Modal opened={!!edit} onClose={() => setEdit(null)} title={edit?.id ? `${t('edit')} ${String(edit.code)}` : 'New leave type'} size="lg" centered>
        {edit ? <Stack>
          {locked ? <Text size="xs" c="dimmed">🔒 {t('lockedStatutory')}: {CITATION[String(edit.code)] ?? ''}. Entitlement fields cannot be changed.</Text> : null}
          <SimpleGrid cols={{ base: 1, sm: 2 }}>
            <TextInput label={t('code')} value={String(edit.code ?? '')} onChange={(e) => set('code', e.currentTarget.value.toUpperCase())} disabled={!!edit.id} required />
            <TextInput label="Name" value={String(edit.nameEn ?? '')} onChange={(e) => set('nameEn', e.currentTarget.value)} required />
            <TextInput label="नाम" value={String(edit.nameNe ?? '')} onChange={(e) => set('nameNe', e.currentTarget.value)} />
            <Switch label="Paid" checked={!!edit.paid} onChange={(e) => set('paid', e.currentTarget.checked)} disabled={locked} />
            <Select label="Accrual basis" value={String(edit.accrualBasis)} onChange={(v) => v && set('accrualBasis', v)} allowDeselect={false} disabled={locked} data={[{ value: 'none', label: 'None (allocated manually)' }, { value: 'worked_days', label: 'Per worked days' }, { value: 'monthly', label: 'Monthly' }, { value: 'annual', label: 'Annual (on Shrawan 1)' }]} />
            {num('accrualDays', 'Days per accrual unit', { disabled: locked, decimalScale: 2 })}
            {num('accrualPerWorkedDays', 'Worked days per accrual (e.g. 20)', { disabled: locked })}
            {num('maxBalance', 'Maximum balance', { disabled: locked, decimalScale: 1 })}
            <Switch label="Carry forward" checked={!!edit.carryForward} onChange={(e) => set('carryForward', e.currentTarget.checked)} />
            <Switch label="Encashable at separation" checked={!!edit.encashable} onChange={(e) => set('encashable', e.currentTarget.checked)} />
            {num('certificateAfterDays', 'Certificate required after (days)')}
            <Switch label="Allow half day" checked={!!edit.allowHalfDay} onChange={(e) => set('allowHalfDay', e.currentTarget.checked)} />
            {num('maxConsecutiveDays', 'Max consecutive days')}
            {num('minServiceDays', 'Minimum service (days)')}
            {num('negativeBalanceLimit', 'Negative balance allowed (days)', { decimalScale: 1 })}
            <Select label="Applies to" value={String(edit.genderScope)} onChange={(v) => v && set('genderScope', v)} allowDeselect={false} disabled={locked} data={[{ value: 'all', label: 'Everyone' }, { value: 'female', label: 'Women' }, { value: 'male', label: 'Men' }]} />
            <Switch label="Sandwich rule (count weekly offs/holidays between leave days)" checked={!!edit.sandwichRule} onChange={(e) => set('sandwichRule', e.currentTarget.checked)} />
            {num('expiryDays', 'Expires after (days)', { disabled: locked })}
          </SimpleGrid>
          <Group justify="flex-end"><Button loading={busy} onClick={save}>{t('save')}</Button></Group>
        </Stack> : null}
      </Modal>
    </Stack>
  );
}
