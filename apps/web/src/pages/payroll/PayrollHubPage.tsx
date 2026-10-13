import { Button, Group, Modal, NumberInput, Select, Stack, Text, TextInput } from '@mantine/core';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { payroll as payApi } from '../../api';
import type { PayrollRun } from '../../api/types';
import { BsMonthPicker } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { EmployeePicker } from '../../components/EmployeePicker';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { useI18n } from '../../i18n';
import { bsMonthLabel, fmtMoney, safeAdToBs, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

/** Payroll hub (plan §13.6): pick a BS period, open or create a run, then follow the five-step wizard. */
export default function PayrollHubPage() {
  const { t, lang, numerals } = useI18n();
  const nav = useNavigate();
  const periods = useQuery(() => payApi.periods(), []);
  const runs = useQuery(() => payApi.runs(), []);
  const bs = safeAdToBs(todayNpt());
  const [ym, setYm] = useState({ year: bs?.year ?? 2083, month: bs?.month ?? 1 });
  const [open, setOpen] = useState(false); const [runType, setRunType] = useState('regular'); const [refRun, setRefRun] = useState<string | null>(null); const [emp, setEmp] = useState<string | null>(null); const [noticePay, setNoticePay] = useState(''); const [busy, setBusy] = useState(false);
  const [bonus, setBonus] = useState(false); const [profit, setProfit] = useState<number | string>(0); const [fy, setFy] = useState<number | string>(bs ? (bs.month >= 4 ? bs.year : bs.year - 1) - 1 : 2082);
  const lockedRuns = (runs.data?.runs ?? []).filter((r) => r.locked_at);
  const createRun = async () => {
    setBusy(true);
    try {
      const p = await payApi.createPeriod(ym.year, ym.month);
      const r = await payApi.createRun({ periodId: p.period.id, runType, ...(refRun ? { referencesRunId: refRun } : {}), ...(emp ? { employeeIds: [emp] } : {}), ...(noticePay ? { noticePay } : {}) });
      toast.ok('Run created. Start with the pre-flight checklist.'); setOpen(false); nav(`/payroll/runs/${r.id}`);
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  const createBonus = async () => {
    setBusy(true);
    try { const p = await payApi.createPeriod(ym.year, ym.month); const r = await payApi.bonusRun({ periodId: p.period.id, fiscalYearStart: Number(fy), netProfit: String(profit) }); toast.ok(`Bonus pool ${fmtMoney(r.pool)}.`); setBonus(false); nav(`/payroll/runs/${r.id}`); }
    catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Stack>
      <PageHeader title={t('payroll')} subtitle={lang === 'ne' ? 'पूर्व-जाँच → इनपुट → गणना र समीक्षा → स्वीकृति → भुक्तानी र प्रकाशन' : 'Pre-flight → Inputs → Calculate & review → Approve → Disburse & publish. A locked run is never edited; corrections are supplementary runs.'}
        actions={<Group gap="xs"><BsMonthPicker value={ym} onChange={setYm} /><Button variant="light" onClick={() => setBonus(true)}>Bonus run</Button><Button onClick={() => setOpen(true)}>{t('runPayroll')}</Button></Group>} />
      <DataTable<PayrollRun> rows={runs.data?.runs ?? []} rowKey={(r) => r.id} loading={runs.loading} error={runs.error} emptyTitle="No payroll runs yet." emptyHint="Create a run for the current BS month to begin." onRowClick={(r) => nav(`/payroll/runs/${r.id}`)}
        columns={[
          { key: 'period', header: t('month'), render: (r) => <><Text size="sm" fw={500}>{bsMonthLabel(r.bs_year ?? 0, r.bs_month ?? 1, lang, numerals)}</Text><Text size="xs" c="dimmed">{r.run_type}</Text></> },
          { key: 'status', header: t('status'), render: (r) => <StatusBadge status={r.status} /> },
          { key: 'n', header: 'Payslips', render: (r) => r.totals?.payslips ?? '—', align: 'right' },
          { key: 'gross', header: t('grossPay'), render: (r) => r.totals?.gross ? fmtMoney(r.totals.gross, { numerals }) : '—', align: 'right' },
          { key: 'net', header: t('netPay'), render: (r) => r.totals?.net ? fmtMoney(r.totals.net, { numerals }) : '—', align: 'right' },
          { key: 'when', header: 'Created', render: (r) => new Date(r.created_at).toLocaleDateString() },
        ]} />
      <Text size="xs" c="dimmed">{periods.data?.periods.length ?? 0} period(s) opened so far.</Text>
      <Modal opened={open} onClose={() => setOpen(false)} title={`${t('runPayroll')} · ${bsMonthLabel(ym.year, ym.month, lang, numerals)}`} centered>
        <Stack>
          <Select label="Run type" value={runType} onChange={(v) => v && setRunType(v)} allowDeselect={false} data={[{ value: 'regular', label: 'Regular monthly' }, { value: 'supplementary', label: 'Supplementary (correction after lock)' }, { value: 'arrears', label: 'Arrears (salary change in a locked period)' }, { value: 'off_cycle', label: 'Off-cycle' }, { value: 'final_settlement', label: 'Final settlement (one employee)' }]} />
          {runType === 'arrears' || runType === 'supplementary' ? <Select label="References locked run" value={refRun} onChange={setRefRun} data={lockedRuns.map((r) => ({ value: r.id, label: `${r.bs_year}-${r.bs_month} ${r.run_type}` }))} required={runType === 'arrears'} /> : null}
          {runType === 'final_settlement' ? <><EmployeePicker value={emp} onChange={setEmp} status="all" required /><TextInput label="Notice pay (+) or deduction (−), NPR" value={noticePay} onChange={(e) => setNoticePay(e.currentTarget.value)} placeholder="0.00" /></> : null}
          <Group justify="flex-end"><Button loading={busy} onClick={createRun}>{t('create')}</Button></Group>
        </Stack>
      </Modal>
      <Modal opened={bonus} onClose={() => setBonus(false)} title="Bonus Act run" centered>
        <Stack>
          <Text size="sm">Pool = 10% of assessed net profit; shares by wages; caps of 8 months (pay ≤ 39,100) or 6 months; eligibility ≥ half the fiscal year's working days (Bonus Act Sec 5–7).</Text>
          <NumberInput label="Fiscal year start (BS)" value={fy} onChange={setFy} />
          <NumberInput label="Assessed net profit (NPR)" value={profit} onChange={setProfit} thousandSeparator="," min={0} />
          <Group justify="flex-end"><Button loading={busy} onClick={createBonus}>{t('create')}</Button></Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
