import { Alert, Badge, Button, Drawer, Group, NumberInput, Select, SimpleGrid, Stack, Switch, Text, TextInput } from '@mantine/core';
import { IconLock, IconPlus } from '@tabler/icons-react';
import { useState } from 'react';
import { leave as leaveApi } from '../../api';
import type { LeaveType } from '../../api/types';
import { DataTable } from '../../components/DataTable';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const ICON = { size: 16, stroke: 1.6 } as const;

const CITATION: Record<string, string> = { HOME: 'Labour Act 2074 Sec 43, 49: 1 day per 20 worked, cap 90', SICK: 'Sec 44: 12/yr, cap 45, certificate over 3 days', MATERNITY: 'Sec 45: 14 weeks, 60 paid days', PATERNITY: 'Sec 45(7): 15 days', MOURNING: 'Sec 48: 13 days', SUBSTITUTE: 'Sec 42: within 21 days', SPECIAL_UNPAID: 'Sec 80(3)' };

function accrualText(l: LeaveType): string {
  if (l.accrual_basis === 'worked_days') return `${l.accrual_days} day per ${l.accrual_per_worked_days} worked`;
  if (l.accrual_basis === 'monthly') return `${l.accrual_days} days a month`;
  if (l.accrual_basis === 'annual') return `${l.accrual_days} days a year`;
  return 'Allocated manually';
}

export default function LeaveTypesPage() {
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
      toast.ok('Leave policy saved.'); setEdit(null); await q.reload();
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  const open = (lt?: LeaveType) => setEdit(lt ? { id: lt.id, statutory: lt.statutory, code: lt.code, nameEn: lt.name_en, nameNe: lt.name_ne ?? '', paid: lt.paid, accrualBasis: lt.accrual_basis, accrualDays: Number(lt.accrual_days), accrualPerWorkedDays: lt.accrual_per_worked_days ?? undefined, maxBalance: lt.max_balance === null ? undefined : Number(lt.max_balance), carryForward: lt.carry_forward, encashable: lt.encashable, certificateAfterDays: lt.certificate_after_days ?? undefined, allowHalfDay: lt.allow_half_day, maxConsecutiveDays: lt.max_consecutive_days ?? undefined, minServiceDays: lt.min_service_days, negativeBalanceLimit: Number(lt.negative_balance_limit), genderScope: lt.gender_scope, sandwichRule: lt.sandwich_rule, expiryDays: lt.expiry_days ?? undefined } : { code: '', nameEn: '', nameNe: '', paid: true, accrualBasis: 'annual', accrualDays: 0, carryForward: false, encashable: false, allowHalfDay: true, minServiceDays: 0, negativeBalanceLimit: 0, genderScope: 'all', sandwichRule: false, statutory: false });
  const set = (k: string, v: unknown) => setEdit((e) => e && ({ ...e, [k]: v }));
  const num = (k: string, label: string, extra: Record<string, unknown> = {}) => <NumberInput label={label} value={(edit?.[k] as number | undefined) ?? ''} onChange={(v) => set(k, v === '' ? undefined : Number(v))} min={0} {...extra} />;
  const sw = (k: string, label: string, description?: string) => <Switch label={label} {...(description ? { description } : {})} checked={!!edit?.[k]} onChange={(e) => set(k, e.currentTarget.checked)} />;
  return (
    <Stack gap="lg">
      <PageHeader title="Leave policies" subtitle="Statutory leave follows the Labour Act and is locked with its citation. Company leave types are fully editable."
        actions={<Button leftSection={<IconPlus {...ICON} />} onClick={() => open()}>New leave type</Button>} />
      <DataTable<LeaveType> rows={q.data?.types ?? []} rowKey={(l) => l.id} loading={q.loading} error={q.error}
        emptyTitle="No leave types yet" emptyHint="Create a leave type to start tracking balances." onRowClick={(l) => open(l)}
        columns={[
          {
            key: 'name', header: 'Leave type', render: (l) => (
              <Group gap={8} wrap="nowrap">
                <div>
                  <Text size="sm" fw={500}>{l.name_en}</Text>
                  <Text size="xs" c="dimmed" ff="monospace">{l.code}</Text>
                </div>
                {l.statutory ? <Badge size="sm" variant="light" color="gray" leftSection={<IconLock size={12} stroke={1.6} />}>Statutory</Badge> : null}
              </Group>
            ),
          },
          { key: 'paid', header: 'Pay', render: (l) => <Text size="sm" c={l.paid ? 'inherit' : 'dimmed'}>{l.paid ? 'Paid' : 'Unpaid'}</Text> },
          { key: 'accrual', header: 'Accrual', render: (l) => <Text size="sm">{accrualText(l)}</Text> },
          { key: 'cap', header: 'Max balance', align: 'right', render: (l) => <span className="num">{l.max_balance ?? '—'}</span> },
          { key: 'cite', header: 'Basis', render: (l) => <Text size="xs" c="dimmed">{CITATION[l.code] ?? 'Company policy'}</Text> },
        ]} />
      <Drawer opened={!!edit} onClose={() => setEdit(null)} position="right" size="md" title={<Text fw={600}>{edit?.id ? `Edit ${String(edit.nameEn || edit.code)}` : 'New leave type'}</Text>}>
        {edit ? (
          <Stack gap="lg">
            {locked ? <Alert variant="light" color="gray" icon={<IconLock {...ICON} />} title="Statutory leave">{CITATION[String(edit.code)] ?? 'Set by the Labour Act'}. Entitlement fields cannot be changed.</Alert> : null}
            <div>
              <SectionTitle>Basics</SectionTitle>
              <SimpleGrid cols={{ base: 1, sm: 2 }}>
                <TextInput label="Code" value={String(edit.code ?? '')} onChange={(e) => set('code', e.currentTarget.value.toUpperCase())} disabled={!!edit.id} required />
                <TextInput label="Name" value={String(edit.nameEn ?? '')} onChange={(e) => set('nameEn', e.currentTarget.value)} required />
                <Select label="Applies to" value={String(edit.genderScope)} onChange={(v) => v && set('genderScope', v)} allowDeselect={false} disabled={locked} data={[{ value: 'all', label: 'Everyone' }, { value: 'female', label: 'Women' }, { value: 'male', label: 'Men' }]} />
                <Switch label="Paid leave" mt={{ sm: 30 }} checked={!!edit.paid} onChange={(e) => set('paid', e.currentTarget.checked)} disabled={locked} />
              </SimpleGrid>
            </div>
            <div>
              <SectionTitle>Entitlement</SectionTitle>
              <SimpleGrid cols={{ base: 1, sm: 2 }}>
                <Select label="Accrual basis" value={String(edit.accrualBasis)} onChange={(v) => v && set('accrualBasis', v)} allowDeselect={false} disabled={locked} data={[{ value: 'none', label: 'None (allocated manually)' }, { value: 'worked_days', label: 'Per worked days' }, { value: 'monthly', label: 'Monthly' }, { value: 'annual', label: 'Annual (on Shrawan 1)' }]} />
                {num('accrualDays', 'Days per accrual', { disabled: locked, decimalScale: 2 })}
                {num('accrualPerWorkedDays', 'Worked days per accrual', { disabled: locked, description: 'For example 20' })}
                {num('maxBalance', 'Maximum balance', { disabled: locked, decimalScale: 1 })}
                {num('expiryDays', 'Expires after (days)', { disabled: locked })}
                {num('negativeBalanceLimit', 'Negative balance allowed (days)', { decimalScale: 1 })}
              </SimpleGrid>
            </div>
            <div>
              <SectionTitle>Requests</SectionTitle>
              <SimpleGrid cols={{ base: 1, sm: 2 }}>
                {num('certificateAfterDays', 'Certificate needed after (days)')}
                {num('maxConsecutiveDays', 'Max consecutive days')}
                {num('minServiceDays', 'Minimum service (days)')}
              </SimpleGrid>
              <Stack gap="sm" mt="md">
                {sw('allowHalfDay', 'Allow half days')}
                {sw('sandwichRule', 'Sandwich rule', 'Count weekly offs and holidays that fall between leave days')}
                {sw('carryForward', 'Carry forward', 'Unused balance moves to the next leave year')}
                {sw('encashable', 'Encash at separation', 'Unused balance is paid out when the employee leaves')}
              </Stack>
            </div>
            <Group justify="flex-end">
              <Button variant="default" onClick={() => setEdit(null)}>Cancel</Button>
              <Button loading={busy} onClick={save}>Save leave type</Button>
            </Group>
          </Stack>
        ) : null}
      </Drawer>
    </Stack>
  );
}
