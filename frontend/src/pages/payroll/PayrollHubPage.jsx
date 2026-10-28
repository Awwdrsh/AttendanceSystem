import { Button, Group, Modal, NumberInput, Select, SimpleGrid, Stack, Text, TextInput } from '@mantine/core';
import { IconCoins, IconPlus, IconReceipt2 } from '@tabler/icons-react';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { payroll as payApi } from '../../api';
import { BsMonthPicker } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { EmployeePicker } from '../../components/EmployeePicker';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { bsMonthLabel, fmtAd, fmtMoney, safeAdToBs, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const RUN_TYPE_LABEL = {
  regular: 'Regular',
  supplementary: 'Supplementary',
  arrears: 'Arrears',
  off_cycle: 'Off-cycle',
  final_settlement: 'Final settlement',
  bonus: 'Bonus',
};
const runTypeLabel = (s) => RUN_TYPE_LABEL[s] ?? s.replace(/_/g, ' ');

/** Pay runs: pick a fiscal month, create a run, then follow the run's step-by-step flow. */
export default function PayrollHubPage() {
  const nav = useNavigate();
  const periods = useQuery(() => payApi.periods(), []);
  const runs = useQuery(() => payApi.runs(), []);
  const bs = safeAdToBs(todayNpt());
  const [ym, setYm] = useState({ year: bs?.year ?? 2083, month: bs?.month ?? 1 });
  const [open, setOpen] = useState(false);
  const [runType, setRunType] = useState('regular');
  const [refRun, setRefRun] = useState(null);
  const [emp, setEmp] = useState(null);
  const [noticePay, setNoticePay] = useState('');
  const [busy, setBusy] = useState(false);
  const [bonus, setBonus] = useState(false);
  const [profit, setProfit] = useState(0);
  const [fy, setFy] = useState(bs ? (bs.month >= 4 ? bs.year : bs.year - 1) - 1 : 2082);
  const rows = runs.data?.runs ?? [];
  const lockedRuns = rows.filter((r) => r.locked_at);
  const createRun = async () => {
    setBusy(true);
    try {
      const p = await payApi.createPeriod(ym.year, ym.month);
      const r = await payApi.createRun({
        periodId: p.period.id,
        runType,
        ...(refRun ? { referencesRunId: refRun } : {}),
        ...(emp ? { employeeIds: [emp] } : {}),
        ...(noticePay ? { noticePay } : {}),
      });
      toast.ok('Pay run created. Start with the pre-flight checks.');
      setOpen(false);
      nav(`/payroll/runs/${r.id}`);
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  const createBonus = async () => {
    setBusy(true);
    try {
      const p = await payApi.createPeriod(ym.year, ym.month);
      const r = await payApi.bonusRun({ periodId: p.period.id, fiscalYearStart: Number(fy), netProfit: String(profit) });
      toast.ok(`Bonus run created with a pool of ${fmtMoney(r.pool)}.`);
      setBonus(false);
      nav(`/payroll/runs/${r.id}`);
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  const periodCount = periods.data?.periods.length ?? 0;
  const empty = !runs.loading && !runs.error && rows.length === 0;

  return (
    <Stack gap="lg">
      <PageHeader
        title="Pay runs"
        subtitle="Each run moves through pre-flight, inputs, review, HR approval and lock, then gives you the salary sheet. Locked runs never change; corrections go in a supplementary run."
        actions={
          <>
            <Button variant="default" leftSection={<IconCoins size={16} stroke={1.6} />} onClick={() => setBonus(true)}>
              Bonus run
            </Button>
            <Button leftSection={<IconPlus size={16} stroke={1.6} />} onClick={() => setOpen(true)}>
              New pay run
            </Button>
          </>
        }
      />

      {empty ? (
        <EmptyState
          icon={<IconReceipt2 size={20} stroke={1.6} />}
          title="No pay runs yet"
          hint="A pay run calculates every employee's payslip for one month from their salary structure and locked attendance. Once approved and locked, it produces the salary sheet and the SSF sheet, and employees can see their payslips."
          action={{ label: 'Create your first pay run', onClick: () => setOpen(true) }}
        />
      ) : (
        <DataTable
          rows={rows}
          rowKey={(r) => r.id}
          loading={runs.loading}
          error={runs.error}
          onRowClick={(r) => nav(`/payroll/runs/${r.id}`)}
          columns={[
            {
              key: 'period',
              header: 'Period',
              render: (r) => (
                <Text size="sm" fw={500}>
                  {bsMonthLabel(r.bs_year ?? 0, r.bs_month ?? 1)}
                </Text>
              ),
            },
            {
              key: 'type',
              header: 'Run type',
              render: (r) => (
                <Text size="sm" c="dimmed">
                  {runTypeLabel(r.run_type)}
                </Text>
              ),
            },
            { key: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} /> },
            { key: 'n', header: 'Headcount', align: 'right', render: (r) => <span className="num">{r.totals?.payslips ?? '—'}</span> },
            {
              key: 'gross',
              header: 'Gross',
              align: 'right',
              render: (r) => <span className="num">{r.totals?.gross ? fmtMoney(r.totals.gross) : '—'}</span>,
            },
            {
              key: 'net',
              header: 'Net',
              align: 'right',
              render: (r) => (
                <span className="num" style={{ fontWeight: 500 }}>
                  {r.totals?.net ? fmtMoney(r.totals.net) : '—'}
                </span>
              ),
            },
            {
              key: 'when',
              header: 'Created',
              render: (r) => (
                <Text size="sm" c="dimmed">
                  {fmtAd(r.created_at)}
                </Text>
              ),
            },
          ]}
        />
      )}
      {periodCount > 0 ? (
        <Text size="xs" c="dimmed">
          {periodCount} pay {periodCount === 1 ? 'period' : 'periods'} opened so far.
        </Text>
      ) : null}

      <Modal opened={open} onClose={() => setOpen(false)} title="New pay run" centered>
        <Stack>
          <BsMonthPicker label="Pay period" value={ym} onChange={setYm} />
          <Select
            label="Run type"
            value={runType}
            onChange={(v) => v && setRunType(v)}
            allowDeselect={false}
            data={[
              { value: 'regular', label: 'Regular monthly' },
              { value: 'supplementary', label: 'Supplementary (correction after lock)' },
              { value: 'arrears', label: 'Arrears (salary change in a locked period)' },
              { value: 'off_cycle', label: 'Off-cycle' },
              { value: 'final_settlement', label: 'Final settlement (one employee)' },
            ]}
          />
          {runType === 'arrears' || runType === 'supplementary' ? (
            <Select
              label="Corrects locked run"
              description="The locked run this one adjusts."
              value={refRun}
              onChange={setRefRun}
              data={lockedRuns.map((r) => ({
                value: r.id,
                label: `${bsMonthLabel(r.bs_year ?? 0, r.bs_month ?? 1)}, ${runTypeLabel(r.run_type).toLowerCase()}`,
              }))}
              required={runType === 'arrears'}
            />
          ) : null}
          {runType === 'final_settlement' ? (
            <>
              <EmployeePicker value={emp} onChange={setEmp} status="all" required />
              <TextInput
                label="Notice pay (NPR)"
                description="Positive to pay, negative to deduct."
                value={noticePay}
                onChange={(e) => setNoticePay(e.currentTarget.value)}
                placeholder="0.00"
              />
            </>
          ) : null}
          <Group justify="flex-end" mt="xs">
            <Button variant="default" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button loading={busy} onClick={createRun}>
              Create pay run
            </Button>
          </Group>
        </Stack>
      </Modal>

      <Modal opened={bonus} onClose={() => setBonus(false)} title="Bonus run" centered>
        <Stack>
          <Text size="sm" c="dimmed">
            The pool is 10% of assessed net profit, shared in proportion to wages. Each share is capped at 8 months' pay (for pay up to
            39,100) or 6 months'. Employees qualify after working at least half the fiscal year (Bonus Act, sections 5 to 7).
          </Text>
          <BsMonthPicker label="Pay period" value={ym} onChange={setYm} />
          <SimpleGrid cols={{ base: 1, sm: 2 }}>
            <NumberInput label="Fiscal year start" description="Nepali year, e.g. 2082" value={fy} onChange={setFy} />
            <NumberInput label="Assessed net profit (NPR)" value={profit} onChange={setProfit} thousandSeparator="," min={0} />
          </SimpleGrid>
          <Group justify="flex-end" mt="xs">
            <Button variant="default" onClick={() => setBonus(false)}>
              Cancel
            </Button>
            <Button loading={busy} onClick={createBonus}>
              Create bonus run
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
