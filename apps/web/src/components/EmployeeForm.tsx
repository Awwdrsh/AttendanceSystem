import { Button, Divider, Group, MultiSelect, Select, SimpleGrid, Stack, Switch, Text, TextInput } from '@mantine/core';
import { IconLock } from '@tabler/icons-react';
import { useEffect, useState, type ReactNode } from 'react';
import { settings as settingsApi, shifts as shiftApi } from '../api';
import type { Employee } from '../api/types';
import { DateField } from './BsDatePicker';

export type EmployeeDraft = Record<string, unknown>;

const DOW = [['0', 'Sunday'], ['1', 'Monday'], ['2', 'Tuesday'], ['3', 'Wednesday'], ['4', 'Thursday'], ['5', 'Friday'], ['6', 'Saturday']];
const FISCAL_MONTHS = ['Baishakh', 'Jestha', 'Ashadh', 'Shrawan', 'Bhadra', 'Ashwin', 'Kartik', 'Mangsir', 'Poush', 'Magh', 'Falgun', 'Chaitra'];

function Section({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <Stack gap="sm">
      <div>
        <Text fw={600} size="sm">{title}</Text>
        {hint ? <Text size="xs" c="dimmed">{hint}</Text> : null}
      </div>
      <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="md" verticalSpacing="sm">{children}</SimpleGrid>
    </Stack>
  );
}

/** Employee master form. PAN and bank account are write-only here; HR reveals them from the profile when needed. */
export function EmployeeForm({ initial, onSubmit, busy, submitLabel = 'Save changes' }: { initial?: Employee | null; onSubmit: (draft: EmployeeDraft) => Promise<void>; busy?: boolean; submitLabel?: string }) {
  const [d, setD] = useState<EmployeeDraft>({});
  const [branches, setBranches] = useState<Array<{ value: string; label: string }>>([]);
  const [shifts, setShifts] = useState<Array<{ value: string; label: string }>>([]);
  useEffect(() => {
    settingsApi.branches().then((r) => setBranches(r.branches.map((b) => ({ value: b.id, label: `${b.name_en} (${b.code})` })))).catch(() => undefined);
    shiftApi.list().then((r) => setShifts(r.shifts.map((s) => ({ value: s.id, label: `${s.code}, ${s.start.slice(0, 5)} to ${s.end.slice(0, 5)}` })))).catch(() => undefined);
  }, []);
  useEffect(() => {
    if (!initial) { setD({ regime: 'ssf', otEligible: true, weeklyOffDays: [6], employmentType: 'regular', isResident: true }); return; }
    setD({ empCode: initial.emp_code, nameEn: initial.name_en, gender: initial.gender ?? undefined, dob: initial.dob ?? undefined, maritalStatus: initial.marital_status ?? undefined,
      joinDate: initial.join_date ?? undefined, exitDate: initial.exit_date ?? undefined, designation: initial.designation ?? '', grade: initial.grade ?? '', phone: initial.phone ?? '', email: initial.email ?? '',
      otEligible: initial.ot_eligible, weeklyOffDays: initial.weekly_off_days, regime: initial.regime, ssfId: initial.ssf_id ?? '', pfId: initial.pf_id ?? '', citId: initial.cit_id ?? '',
      bankCode: initial.bank_code ?? '', bankBranch: initial.bank_branch ?? '', isResident: initial.is_resident, remoteAreaClass: initial.remote_area_class ?? undefined, disability: initial.disability,
      festivalMonth: initial.festival_month ?? undefined, probationEnd: initial.probation_end ?? undefined, branchId: initial.branch_id ?? undefined, managerId: initial.manager_id ?? undefined,
      employmentType: initial.employment_type ?? 'regular', shiftId: initial.shift_id ?? undefined });
  }, [initial]);
  const s = (k: string) => (v: unknown) => setD((p) => ({ ...p, [k]: v }));
  const str = (k: string) => (d[k] as string | undefined) ?? '';
  const submit = async () => {
    const out: EmployeeDraft = {};
    for (const [k, v] of Object.entries(d)) { if (v === '' || v === undefined) continue; out[k] = v; }
    if (initial && d.exitDate === undefined && initial.exit_date) out.exitDate = null;
    await onSubmit(out);
  };
  return (
    <Stack gap="lg">
      <Section title="Personal details">
        <TextInput label="Full name" required value={str('nameEn')} onChange={(e) => s('nameEn')(e.currentTarget.value)} />
        <TextInput label="Employee code" required description="Must match the code on the attendance terminal." value={str('empCode')} onChange={(e) => s('empCode')(e.currentTarget.value)} />
        <TextInput label="Email" type="email" value={str('email')} onChange={(e) => s('email')(e.currentTarget.value)} />
        <TextInput label="Phone" value={str('phone')} onChange={(e) => s('phone')(e.currentTarget.value)} />
        <Select label="Gender" value={(d.gender as string) ?? null} onChange={s('gender')} data={[{ value: 'male', label: 'Male' }, { value: 'female', label: 'Female' }, { value: 'other', label: 'Other' }]} clearable />
        <DateField label="Date of birth" value={(d.dob as string) ?? null} onChange={s('dob')} />
        <Select label="Marital status" description="Sets the income tax slab." value={(d.maritalStatus as string) ?? null} onChange={s('maritalStatus')} data={[{ value: 'single', label: 'Single' }, { value: 'couple', label: 'Couple' }]} clearable />
      </Section>
      <Divider />
      <Section title="Job">
        <TextInput label="Designation" value={str('designation')} onChange={(e) => s('designation')(e.currentTarget.value)} />
        <TextInput label="Grade" value={str('grade')} onChange={(e) => s('grade')(e.currentTarget.value)} />
        <DateField label="Join date" value={(d.joinDate as string) ?? null} onChange={s('joinDate')} />
        <DateField label="Probation ends" value={(d.probationEnd as string) ?? null} onChange={s('probationEnd')} />
        <Select label="Employment type" value={(d.employmentType as string) ?? 'regular'} onChange={s('employmentType')} allowDeselect={false} data={[{ value: 'regular', label: 'Regular' }, { value: 'work_based', label: 'Work-based' }, { value: 'time_based', label: 'Time-based' }, { value: 'casual', label: 'Casual (up to 7 days a month)' }, { value: 'part_time', label: 'Part-time (under 35 h a week)' }]} />
        <Select label="Branch" value={(d.branchId as string) ?? null} onChange={s('branchId')} data={branches} clearable />
        {initial ? <DateField label="Exit date" value={(d.exitDate as string) ?? null} onChange={s('exitDate')} description="Setting this marks the employee as exited." /> : null}
      </Section>
      <Divider />
      <Section title="Schedule">
        <Select label="Shift" value={(d.shiftId as string) ?? null} onChange={s('shiftId')} data={shifts} clearable />
        <MultiSelect label="Weekly off days" value={((d.weeklyOffDays as number[]) ?? [6]).map(String)} onChange={(v) => s('weeklyOffDays')(v.map(Number))} data={DOW.map(([v, l]) => ({ value: v!, label: l! }))} />
        <Switch label="Eligible for overtime" description="Labour Act Sec 31. Excluding someone needs a documented Sec 31(2) basis." checked={!!d.otEligible} onChange={(e) => s('otEligible')(e.currentTarget.checked)} />
      </Section>
      <Divider />
      <Section title="Tax and social security">
        <Select label="Retirement scheme" description="SSF: 11% employee, 20% employer." value={(d.regime as string) ?? 'ssf'} onChange={s('regime')} allowDeselect={false} data={[{ value: 'ssf', label: 'SSF' }, { value: 'pf', label: 'PF and gratuity' }, { value: 'none', label: 'None' }]} />
        <TextInput label="SSF ID" value={str('ssfId')} onChange={(e) => s('ssfId')(e.currentTarget.value)} />
        <TextInput label="PF ID" value={str('pfId')} onChange={(e) => s('pfId')(e.currentTarget.value)} />
        <TextInput label="CIT ID" value={str('citId')} onChange={(e) => s('citId')(e.currentTarget.value)} />
        <TextInput label="PAN" description={initial?.has_pan ? 'A PAN is on file. Enter a new one to replace it.' : '9 digits.'} value={str('pan')} onChange={(e) => s('pan')(e.currentTarget.value)} placeholder="•••••••••" inputMode="numeric" />
        <Select label="Remote area class" value={(d.remoteAreaClass as string) ?? null} onChange={s('remoteAreaClass')} data={['A', 'B', 'C', 'D'].map((c) => ({ value: c, label: `Class ${c}` }))} clearable />
        <Select label="Festival allowance month" description="Defaults to Ashwin (Dashain). The employee may choose another month." value={d.festivalMonth ? String(d.festivalMonth) : null} onChange={(v) => s('festivalMonth')(v ? Number(v) : undefined)} data={FISCAL_MONTHS.map((m, i) => ({ value: String(i + 1), label: m }))} clearable />
        <Stack gap="sm" justify="center">
          <Switch label="Resident for tax" checked={d.isResident !== false} onChange={(e) => s('isResident')(e.currentTarget.checked)} />
          <Switch label="Has a disability" description="Adds the extra tax deduction." checked={!!d.disability} onChange={(e) => s('disability')(e.currentTarget.checked)} />
        </Stack>
      </Section>
      <Divider />
      <Section title="Bank">
        <Select label="Bank" value={(d.bankCode as string) ?? null} onChange={s('bankCode')} data={['NMB', 'NABIL', 'NIC_ASIA', 'EVEREST', 'PRABHU', 'GLOBAL_IME', 'OTHER'].map((b) => ({ value: b, label: b === 'OTHER' ? 'Other' : b.replace('_', ' ') }))} clearable />
        <TextInput label="Bank branch" value={str('bankBranch')} onChange={(e) => s('bankBranch')(e.currentTarget.value)} />
        <TextInput label="Account number" description={initial?.bank_account_last4 ? `Account ending ${initial.bank_account_last4} is on file. Enter a new one to replace it.` : undefined} value={str('bankAccount')} onChange={(e) => s('bankAccount')(e.currentTarget.value)} placeholder="••••••••••" />
      </Section>
      <Group justify="space-between" wrap="wrap" gap="sm">
        <Group gap={6} wrap="nowrap"><IconLock size={14} stroke={1.6} color="var(--hj-muted)" /><Text size="xs" c="dimmed">PAN and bank account are encrypted. Every reveal is logged.</Text></Group>
        <Button loading={busy ?? false} onClick={submit} disabled={!str('empCode') || !str('nameEn')}>{submitLabel}</Button>
      </Group>
    </Stack>
  );
}
