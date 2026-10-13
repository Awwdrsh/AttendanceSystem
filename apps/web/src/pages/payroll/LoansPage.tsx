import { Button, Group, Modal, NumberInput, Select, Stack, Text, TextInput } from '@mantine/core';
import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { payroll as payApi } from '../../api';
import type { Loan } from '../../api/types';
import { DataTable } from '../../components/DataTable';
import { EmployeePicker } from '../../components/EmployeePicker';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { useI18n } from '../../i18n';
import { bsMonthLabel, fmtMoney, safeAdToBs, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

export default function LoansPage() {
  const { t, lang, numerals } = useI18n();
  const [params] = useSearchParams();
  const [employeeId, setEmployeeId] = useState<string | null>(params.get('employeeId'));
  const q = useQuery(() => payApi.loans(employeeId ?? undefined), [employeeId]);
  const bs = safeAdToBs(todayNpt());
  const [open, setOpen] = useState(false); const [emp, setEmp] = useState<string | null>(null); const [kind, setKind] = useState('loan'); const [principal, setPrincipal] = useState<number | string>(0); const [emi, setEmi] = useState<number | string>(0); const [y, setY] = useState<number | string>(bs?.year ?? 2083); const [m, setM] = useState<number | string>(bs?.month ?? 1); const [note, setNote] = useState(''); const [busy, setBusy] = useState(false);
  return (
    <Stack>
      <PageHeader title={t('loans')} subtitle="EMI deductions apply after statutory items (Labour Act Sec 38(i)); instalments are recorded on each payslip." actions={<Group gap="xs"><EmployeePicker value={employeeId} onChange={setEmployeeId} label="" status="all" /><Button onClick={() => setOpen(true)}>{t('create')}</Button></Group>} />
      <DataTable<Loan> rows={q.data?.loans ?? []} rowKey={(l) => l.id} loading={q.loading} error={q.error} emptyTitle="No loans or advances."
        columns={[{ key: 'emp_code', header: t('code') }, { key: 'kind', header: 'Kind' }, { key: 'principal', header: 'Principal', render: (l) => fmtMoney(l.principal, { numerals }), align: 'right' }, { key: 'emi', header: 'EMI', render: (l) => fmtMoney(l.emi, { numerals }), align: 'right' },
          { key: 'start', header: 'From', render: (l) => bsMonthLabel(l.start_bs_year, l.start_bs_month, lang, numerals) }, { key: 'repaid', header: 'Repaid', render: (l) => `${fmtMoney(l.repaid, { numerals, currency: false })} / ${l.installments} inst.`, align: 'right' }, { key: 'status', header: t('status'), render: (l) => <StatusBadge status={l.status} /> }]} />
      <Modal opened={open} onClose={() => setOpen(false)} title="New loan / advance" centered>
        <Stack>
          <EmployeePicker value={emp} onChange={setEmp} required />
          <Select label="Kind" value={kind} onChange={(v) => v && setKind(v)} allowDeselect={false} data={['loan', 'advance']} />
          <Group grow><NumberInput label="Principal (NPR)" value={principal} onChange={setPrincipal} min={1} thousandSeparator="," /><NumberInput label="EMI (NPR)" value={emi} onChange={setEmi} min={1} thousandSeparator="," /></Group>
          <Group grow><NumberInput label="Start year (BS)" value={y} onChange={setY} /><NumberInput label="Start month" value={m} onChange={setM} min={1} max={12} /></Group>
          <TextInput label="Note" value={note} onChange={(e) => setNote(e.currentTarget.value)} />
          <Text size="xs" c="dimmed">Instalments = ceil(principal ÷ EMI).</Text>
          <Group justify="flex-end"><Button loading={busy} disabled={!emp || !Number(principal) || !Number(emi)} onClick={async () => { setBusy(true); try { const r = await payApi.createLoan({ employeeId: emp, kind, principal: Number(principal).toFixed(2), emi: Number(emi).toFixed(2), startBsYear: Number(y), startBsMonth: Number(m), ...(note ? { note } : {}) }); toast.ok(`Loan created: ${r.installments} instalments.`); setOpen(false); await q.reload(); } catch (e) { toast.err(e); } finally { setBusy(false); } }}>{t('create')}</Button></Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
