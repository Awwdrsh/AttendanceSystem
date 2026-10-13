import { Button, Group, Modal, Select, Stack, Text, TextInput } from '@mantine/core';
import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { payroll as payApi } from '../../api';
import type { SalaryAssignment } from '../../api/types';
import { BsDatePicker } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { EmployeePicker } from '../../components/EmployeePicker';
import { PageHeader } from '../../components/PageHeader';
import { useI18n } from '../../i18n';
import { fmtMoney, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

export default function AssignmentsPage() {
  const { t, numerals } = useI18n();
  const [params] = useSearchParams();
  const [employeeId, setEmployeeId] = useState<string | null>(params.get('employeeId'));
  const q = useQuery(() => payApi.assignments(employeeId ?? undefined), [employeeId]);
  const structs = useQuery(() => payApi.structures(), []);
  const [open, setOpen] = useState(false); const [emp, setEmp] = useState<string | null>(employeeId); const [ver, setVer] = useState<string | null>(null); const [from, setFrom] = useState<string | null>(todayNpt()); const [amounts, setAmounts] = useState<Record<string, string>>({}); const [busy, setBusy] = useState(false);
  const versions = (structs.data?.structures ?? []).flatMap((s) => (s.versions ?? []).map((v) => ({ value: v.id, label: `${s.code} v${v.version}`, codes: v.components.map((c) => c.code) })));
  const codes = versions.find((v) => v.value === ver)?.codes ?? [];
  return (
    <Stack>
      <PageHeader title={t('assignments')} subtitle="Effective-dated. A new assignment closes the previous one the day before; base amounts are decimal strings (never floats). Minimum wage: basic ≥ 12,170 and basic + DA ≥ 19,550 (Notice 2082)." actions={<Group gap="xs"><EmployeePicker value={employeeId} onChange={setEmployeeId} label="" status="all" /><Button onClick={() => { setEmp(employeeId); setOpen(true); }}>Assign</Button></Group>} />
      <DataTable<SalaryAssignment> rows={q.data?.assignments ?? []} rowKey={(a) => `${a.employee_id}-${a.valid}`} loading={q.loading} error={q.error} emptyTitle="No salary assignments." emptyHint="Assign a structure with base amounts before running payroll."
        columns={[{ key: 'emp', header: t('name'), render: (a) => <><Text size="sm" fw={500}>{a.name_en}</Text><Text size="xs" c="dimmed">{a.emp_code}</Text></> }, { key: 'structure', header: 'Structure', render: (a) => `${a.structure_code} v${a.version}` }, { key: 'valid', header: 'Valid' },
          { key: 'amounts', header: 'Base amounts', render: (a) => <Text size="xs">{Object.entries(a.base_amounts).map(([k, v]) => `${k} ${fmtMoney(v, { numerals, currency: false })}`).join(' · ')}</Text> }]} />
      <Modal opened={open} onClose={() => setOpen(false)} title="Assign salary structure" centered>
        <Stack>
          <EmployeePicker value={emp} onChange={setEmp} required />
          <Select label="Structure version" value={ver} onChange={setVer} data={versions.map((v) => ({ value: v.value, label: v.label }))} required />
          <BsDatePicker label="Effective from" value={from} onChange={setFrom} required />
          {codes.map((c) => <TextInput key={c} label={`${c} (NPR, monthly)`} value={amounts[c] ?? ''} onChange={(e) => setAmounts({ ...amounts, [c]: e.currentTarget.value })} placeholder="e.g. 40000.00" />)}
          <Text size="xs" c="dimmed">Leave formula-driven components (SSF, TDS, OT…) blank; only fixed amounts like BASIC, DA, ALLOWANCE need values.</Text>
          <Group justify="flex-end"><Button loading={busy} disabled={!emp || !ver || !from} onClick={async () => { setBusy(true); try { const base: Record<string, string> = {}; for (const [k, v] of Object.entries(amounts)) if (/^\d+(\.\d{1,2})?$/.test(v)) base[k] = v; await payApi.assign({ employeeId: emp!, structureVersionId: ver!, from: from!, baseAmounts: base }); toast.ok('Assigned.'); setOpen(false); await q.reload(); } catch (e) { toast.err(e); } finally { setBusy(false); } }}>{t('save')}</Button></Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
