import { Button, Group, MultiSelect, Select, SimpleGrid, Stack, Switch, Tabs, Text, TextInput } from '@mantine/core';
import { useEffect, useState } from 'react';
import { settings as settingsApi, shifts as shiftApi } from '../api';
import type { Employee } from '../api/types';
import { BsDatePicker } from './BsDatePicker';
import { useI18n } from '../i18n';

export type EmployeeDraft = Record<string, unknown>;

const DOW = [['0', 'Sunday'], ['1', 'Monday'], ['2', 'Tuesday'], ['3', 'Wednesday'], ['4', 'Thursday'], ['5', 'Friday'], ['6', 'Saturday']];

/** Full employee master form (plan §5.1). PAN and bank account are write-only here; HR reveals them from the profile when needed. */
export function EmployeeForm({ initial, onSubmit, busy, submitLabel = 'Save' }: { initial?: Employee | null; onSubmit: (draft: EmployeeDraft) => Promise<void>; busy?: boolean; submitLabel?: string }) {
  const { t } = useI18n();
  const [d, setD] = useState<EmployeeDraft>({});
  const [branches, setBranches] = useState<Array<{ value: string; label: string }>>([]);
  const [shifts, setShifts] = useState<Array<{ value: string; label: string }>>([]);
  useEffect(() => {
    settingsApi.branches().then((r) => setBranches(r.branches.map((b) => ({ value: b.id, label: `${b.code} · ${b.name_en}` })))).catch(() => undefined);
    shiftApi.list().then((r) => setShifts(r.shifts.map((s) => ({ value: s.id, label: `${s.code} ${s.start.slice(0, 5)}–${s.end.slice(0, 5)}` })))).catch(() => undefined);
  }, []);
  useEffect(() => {
    if (!initial) { setD({ regime: 'ssf', otEligible: true, weeklyOffDays: [6], employmentType: 'regular', isResident: true }); return; }
    setD({ empCode: initial.emp_code, nameEn: initial.name_en, nameNe: initial.name_ne ?? '', gender: initial.gender ?? undefined, dob: initial.dob ?? undefined, maritalStatus: initial.marital_status ?? undefined,
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
    <Stack>
      <Tabs defaultValue="basic">
        <Tabs.List><Tabs.Tab value="basic">Basic</Tabs.Tab><Tabs.Tab value="job">Job</Tabs.Tab><Tabs.Tab value="statutory">Statutory & bank</Tabs.Tab></Tabs.List>
        <Tabs.Panel value="basic" pt="sm">
          <SimpleGrid cols={{ base: 1, sm: 2 }}>
            <TextInput label="Employee code" required value={str('empCode')} onChange={(e) => s('empCode')(e.currentTarget.value)} />
            <TextInput label="Name (English)" required value={str('nameEn')} onChange={(e) => s('nameEn')(e.currentTarget.value)} />
            <TextInput label="नाम (नेपाली)" value={str('nameNe')} onChange={(e) => s('nameNe')(e.currentTarget.value)} />
            <Select label="Gender" value={(d.gender as string) ?? null} onChange={s('gender')} data={[{ value: 'male', label: 'Male' }, { value: 'female', label: 'Female' }, { value: 'other', label: 'Other' }]} clearable />
            <BsDatePicker label="Date of birth" value={(d.dob as string) ?? null} onChange={s('dob')} />
            <Select label="Marital status (tax)" value={(d.maritalStatus as string) ?? null} onChange={s('maritalStatus')} data={[{ value: 'single', label: 'Single' }, { value: 'couple', label: 'Couple' }]} clearable />
            <TextInput label="Phone" value={str('phone')} onChange={(e) => s('phone')(e.currentTarget.value)} />
            <TextInput label="Email" type="email" value={str('email')} onChange={(e) => s('email')(e.currentTarget.value)} />
          </SimpleGrid>
        </Tabs.Panel>
        <Tabs.Panel value="job" pt="sm">
          <SimpleGrid cols={{ base: 1, sm: 2 }}>
            <BsDatePicker label="Join date" value={(d.joinDate as string) ?? null} onChange={s('joinDate')} />
            <BsDatePicker label="Probation end" value={(d.probationEnd as string) ?? null} onChange={s('probationEnd')} />
            <TextInput label="Designation" value={str('designation')} onChange={(e) => s('designation')(e.currentTarget.value)} />
            <TextInput label="Grade" value={str('grade')} onChange={(e) => s('grade')(e.currentTarget.value)} />
            <Select label="Employment type" value={(d.employmentType as string) ?? 'regular'} onChange={s('employmentType')} allowDeselect={false} data={[{ value: 'regular', label: 'Regular' }, { value: 'work_based', label: 'Work-based' }, { value: 'time_based', label: 'Time-based' }, { value: 'casual', label: 'Casual (≤ 7 days/month)' }, { value: 'part_time', label: 'Part-time (< 35 h/week)' }]} />
            <Select label="Branch" value={(d.branchId as string) ?? null} onChange={s('branchId')} data={branches} clearable />
            <Select label="Shift" value={(d.shiftId as string) ?? null} onChange={s('shiftId')} data={shifts} clearable />
            <MultiSelect label="Weekly off days" value={((d.weeklyOffDays as number[]) ?? [6]).map(String)} onChange={(v) => s('weeklyOffDays')(v.map(Number))} data={DOW.map(([v, l]) => ({ value: v!, label: l! }))} />
            <Switch label="Overtime eligible (Sec 31)" checked={!!d.otEligible} onChange={(e) => s('otEligible')(e.currentTarget.checked)} description="Exclusion needs a documented Sec 31(2) basis." />
            {initial ? <BsDatePicker label="Exit date" value={(d.exitDate as string) ?? null} onChange={s('exitDate')} description="Setting this marks the employee as exited." /> : null}
          </SimpleGrid>
        </Tabs.Panel>
        <Tabs.Panel value="statutory" pt="sm">
          <SimpleGrid cols={{ base: 1, sm: 2 }}>
            <Select label="Regime" value={(d.regime as string) ?? 'ssf'} onChange={s('regime')} allowDeselect={false} data={[{ value: 'ssf', label: 'SSF (11% + 20%)' }, { value: 'pf', label: 'PF + gratuity' }, { value: 'none', label: 'None' }]} />
            <TextInput label="SSF ID" value={str('ssfId')} onChange={(e) => s('ssfId')(e.currentTarget.value)} />
            <TextInput label="PF ID" value={str('pfId')} onChange={(e) => s('pfId')(e.currentTarget.value)} />
            <TextInput label="CIT ID" value={str('citId')} onChange={(e) => s('citId')(e.currentTarget.value)} />
            <TextInput label={initial?.has_pan ? 'PAN (stored; enter to replace)' : 'PAN (9 digits)'} value={str('pan')} onChange={(e) => s('pan')(e.currentTarget.value)} placeholder="•••••••••" inputMode="numeric" />
            <Select label="Bank" value={(d.bankCode as string) ?? null} onChange={s('bankCode')} data={['NMB', 'NABIL', 'NIC_ASIA', 'EVEREST', 'PRABHU', 'GLOBAL_IME', 'OTHER'].map((b) => ({ value: b, label: b.replace('_', ' ') }))} clearable />
            <TextInput label="Bank branch" value={str('bankBranch')} onChange={(e) => s('bankBranch')(e.currentTarget.value)} />
            <TextInput label={initial?.bank_account_last4 ? `Bank account (stored ****${initial.bank_account_last4}; enter to replace)` : 'Bank account number'} value={str('bankAccount')} onChange={(e) => s('bankAccount')(e.currentTarget.value)} placeholder="••••••••••" />
            <Switch label="Resident for tax" checked={d.isResident !== false} onChange={(e) => s('isResident')(e.currentTarget.checked)} />
            <Select label="Remote area class" value={(d.remoteAreaClass as string) ?? null} onChange={s('remoteAreaClass')} data={['A', 'B', 'C', 'D'].map((c) => ({ value: c, label: `Class ${c}` }))} clearable />
            <Switch label="Disability (extra tax deduction)" checked={!!d.disability} onChange={(e) => s('disability')(e.currentTarget.checked)} />
            <Select label="Festival allowance month" description="Default Ashwin (Dashain); Sec 37(2) lets the employee choose." value={d.festivalMonth ? String(d.festivalMonth) : null} onChange={(v) => s('festivalMonth')(v ? Number(v) : undefined)} data={['Baishakh', 'Jestha', 'Ashadh', 'Shrawan', 'Bhadra', 'Ashwin', 'Kartik', 'Mangsir', 'Poush', 'Magh', 'Falgun', 'Chaitra'].map((m, i) => ({ value: String(i + 1), label: m }))} clearable />
          </SimpleGrid>
          <Text size="xs" c="dimmed" mt="xs">PAN and bank account are encrypted at rest; every reveal is logged.</Text>
        </Tabs.Panel>
      </Tabs>
      <Group justify="flex-end"><Button loading={busy ?? false} onClick={submit} disabled={!str('empCode') || !str('nameEn')} h={44}>{submitLabel || t('save')}</Button></Group>
    </Stack>
  );
}
