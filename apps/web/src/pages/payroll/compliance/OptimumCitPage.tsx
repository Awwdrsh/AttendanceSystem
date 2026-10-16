import { Button, Group, Modal, NumberInput, Select, Stack, Text } from '@mantine/core';
import { BS_MONTH_NAMES_EN } from '@hajiri/bs-calendar';
import { useMemo, useState } from 'react';
import { compliance, type OptimumRow } from '../../../api/compliance';
import { DataTable } from '../../../components/DataTable';
import { StatStrip } from '../../../components/Kpi';
import { PageHeader } from '../../../components/PageHeader';
import { fmtMoney, safeAdToBs, todayNpt } from '../../../lib/format';
import { toast } from '../../../lib/toast';
import { useQuery } from '../../../lib/useQuery';

const money = (v: string | undefined) => fmtMoney(v, { decimals: 0 });
const REASON: Record<string, string> = {
  already_optimal: 'Already lowest tax', no_tax_left: 'No tax left to save', net_pay: 'Limited by net pay', limit: 'Fills the deduction limit',
};

/** HR: for each employee, the monthly CIT that minimises this year's tax, and applying it from a chosen month. */
export default function OptimumCitPage() {
  const q = useQuery(() => compliance.optimumAll(), []);
  const bs = safeAdToBs(todayNpt());
  const [row, setRow] = useState<OptimumRow | null>(null);
  const [amount, setAmount] = useState<number | string>(0);
  const [month, setMonth] = useState<string>(String(bs?.month ?? 4));
  const [busy, setBusy] = useState(false);
  const rows = q.data?.employees ?? [];
  const withSaving = rows.filter((r) => Number(r.annualSaving ?? 0) > 0);
  const total = withSaving.reduce((a, r) => a + Number(r.annualSaving ?? 0), 0);
  // Months from now to Ashadh, in fiscal-year order.
  const months = useMemo(() => {
    const out: Array<{ value: string; label: string }> = [];
    if (!bs) return out;
    for (let m = bs.month, i = 0; i < 12; i++) {
      out.push({ value: String(m), label: BS_MONTH_NAMES_EN[m - 1]! });
      if (m === 3) break;
      m = m === 12 ? 1 : m + 1;
    }
    return out;
  }, [bs?.month]); // eslint-disable-line react-hooks/exhaustive-deps

  const apply = async () => {
    if (!row) return;
    setBusy(true);
    try {
      await compliance.applyCit({ employeeId: row.employeeId, monthlyCit: Number(amount).toFixed(2), fromBsMonth: Number(month) });
      toast.ok(`${row.name}'s CIT is now ${money(String(amount))} a month from ${BS_MONTH_NAMES_EN[Number(month) - 1]}. They were notified.`);
      setRow(null); await q.reload();
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };

  return (
    <Stack gap="lg">
      <PageHeader title="Optimum CIT" subtitle="The Citizen Investment Trust contribution that brings each person's tax lowest this year. Retirement savings (SSF, PF, CIT) reduce taxable income up to one third of income or NPR 5,00,000. Confirm the limits with your accountant." />
      <StatStrip stats={[
        { key: 'n', label: 'Can save tax', value: String(withSaving.length), hint: `of ${rows.length} employees` },
        { key: 'total', label: 'Tax that could be saved', value: money(String(total)), hint: `FY ${q.data?.fiscalYearStart ?? ''}`, ...(total > 0 ? { tone: 'ok' as const } : {}) },
      ]} />
      <DataTable<OptimumRow> rows={[...rows].sort((a, b) => Number(b.annualSaving ?? 0) - Number(a.annualSaving ?? 0))} rowKey={(r) => r.employeeId} loading={q.loading} error={q.error}
        emptyTitle="No salaries set up" emptyHint="Assign salaries under Employee salaries, then come back to see CIT suggestions."
        columns={[
          { key: 'name', header: 'Employee', render: (r) => <><Text size="sm" fw={500}>{r.name}</Text><Text size="xs" c="dimmed">{r.empCode}</Text></> },
          { key: 'current', header: 'CIT now (monthly)', align: 'right', render: (r) => r.available ? <span className="num">{money(r.currentMonthly)}</span> : <Text size="xs" c="dimmed">{r.reason}</Text> },
          { key: 'optimum', header: 'Suggested (monthly)', align: 'right', render: (r) => r.available ? <Text size="sm" fw={600} className="num">{money(r.optimumMonthly)}</Text> : null },
          { key: 'tax', header: 'Tax for the year', align: 'right', render: (r) => r.available ? <><Text size="sm" className="num">{money(r.optimumAnnualTax)}</Text>{Number(r.annualSaving) > 0 ? <Text size="xs" c="dimmed" className="num">now {money(r.currentAnnualTax)}</Text> : null}</> : null },
          { key: 'saving', header: 'Saving', align: 'right', render: (r) => r.available ? <Text size="sm" fw={600} c={Number(r.annualSaving) > 0 ? 'teal' : 'dimmed'} className="num">{money(r.annualSaving)}</Text> : null },
          { key: 'why', header: 'Note', render: (r) => r.available ? <Text size="xs" c="dimmed">{REASON[r.reason ?? ''] ?? ''}</Text> : null },
          { key: 'act', header: '', align: 'right', render: (r) => r.available && Number(r.annualSaving) > 0
            ? <Button size="xs" variant="light" onClick={() => { setRow(r); setAmount(Number(r.optimumMonthly)); setMonth(String(bs?.month ?? 4)); }}>Apply</Button> : null },
        ]} />
      <Modal opened={!!row} onClose={() => setRow(null)} title={`Set CIT for ${row?.name ?? ''}`} centered>
        <Stack>
          <Text size="sm" c="dimmed">This changes the CIT deducted from {row?.name}'s pay from the month you choose until the end of the fiscal year. Talk to them first: the money goes to their CIT account and lowers their take-home pay, but saves {money(row?.annualSaving)} in tax this year.</Text>
          <NumberInput label="Monthly CIT (NPR)" value={amount} onChange={setAmount} min={0} thousandSeparator="," />
          <Select label="Starting from" value={month} onChange={(v) => v && setMonth(v)} data={months} allowDeselect={false} />
          <Group justify="flex-end"><Button variant="default" onClick={() => setRow(null)}>Cancel</Button><Button loading={busy} onClick={apply}>Apply</Button></Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
