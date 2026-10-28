import {
  Alert,
  Button,
  Group,
  Paper,
  ScrollArea,
  SegmentedControl,
  Select,
  SimpleGrid,
  Stack,
  Table,
  Text,
  TextInput,
} from '@mantine/core';
import { IconDownload, IconFileCertificate, IconInfoCircle } from '@tabler/icons-react';
import { useState } from 'react';
import { payroll as payApi } from '../../../api';
import { compliance } from '../../../api/compliance';
import { BsMonthPicker, DateField } from '../../../components/BsDatePicker';
import { EmployeePicker } from '../../../components/EmployeePicker';
import { EmptyState } from '../../../components/EmptyState';
import { StatStrip } from '../../../components/Kpi';
import { PageHeader } from '../../../components/PageHeader';
import { bsMonthLabel, fmtMoney, safeAdToBs, todayNpt } from '../../../lib/format';
import { toast } from '../../../lib/toast';
import { useQuery } from '../../../lib/useQuery';

const ICON = { size: 16, stroke: 1.6 };
const MONEY_KEYS =
  /^(gross|taxable|sst|incomeTax|tds|SSF_EE|SSF_ER|PF_EE|PF_ER|GRATUITY|CIT|ssfTotal|pfTotal|retirementEmployee|retirementEmployer|taxableAfterDeductions|annualTax|balance|previousNet|net|difference|dailyRate|amount)$/;

/** A report table: money columns right-aligned, totals for money columns at the bottom. */
function ReportTable({ result, empty }) {
  if (!result) return null;
  if (!result.rows.length) return <EmptyState compact title={empty} />;
  const cols = result.columns.filter((c) => c.key !== 'employeeId');
  const money = (k) => MONEY_KEYS.test(k);
  return (
    <Paper withBorder style={{ overflow: 'hidden' }}>
      <ScrollArea type="auto">
        <Table striped miw={Math.min(1400, cols.length * 130)} stickyHeader>
          <Table.Thead>
            <Table.Tr>
              {cols.map((c) => (
                <Table.Th key={c.key} style={{ textAlign: money(c.key) ? 'right' : 'left', whiteSpace: 'nowrap' }}>
                  {c.header}
                </Table.Th>
              ))}
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {result.rows.map((r, i) => (
              <Table.Tr key={i}>
                {cols.map((c) => (
                  <Table.Td key={c.key} ta={money(c.key) ? 'right' : 'left'} className={money(c.key) ? 'num' : undefined}>
                    {money(c.key) && r[c.key] !== '' ? fmtMoney(r[c.key], { currency: false }) : r[c.key]}
                  </Table.Td>
                ))}
              </Table.Tr>
            ))}
          </Table.Tbody>
          <Table.Tfoot>
            <Table.Tr>
              {cols.map((c, i) => (
                <Table.Th key={c.key} ta={money(c.key) ? 'right' : 'left'} className="num">
                  {i === 0
                    ? `Total (${result.rows.length})`
                    : money(c.key) && c.key !== 'dailyRate' && c.key !== 'percent'
                      ? fmtMoney(
                          result.rows.reduce((a, r) => a + Number(r[c.key] || 0), 0),
                          { currency: false },
                        )
                      : ''}
                </Table.Th>
              ))}
            </Table.Tr>
          </Table.Tfoot>
        </Table>
      </ScrollArea>
    </Paper>
  );
}

function MonthReport({ kind }) {
  const bs = safeAdToBs(todayNpt());
  const [m, setM] = useState({ year: bs?.year ?? 2083, month: bs ? (bs.month === 1 ? 12 : bs.month - 1) : 5 });
  const q = useQuery(
    () => (kind === 'tax' ? compliance.taxDeposit(m.year, m.month) : compliance.retirementDeposit(m.year, m.month)),
    [kind, m.year, m.month],
  );
  const report = kind === 'tax' ? 'tax-deposit' : 'retirement-deposit';
  return (
    <Stack>
      <Group justify="space-between" align="flex-end" wrap="wrap">
        <BsMonthPicker label="Pay month" value={m} onChange={setM} />
        <Button
          variant="default"
          leftSection={<IconDownload {...ICON} />}
          onClick={() => compliance.monthCsv(report, m.year, m.month).catch((e) => toast.err(e))}
        >
          Download CSV
        </Button>
      </Group>
      <Text size="sm" c="dimmed">
        {kind === 'tax'
          ? 'Tax withheld from every locked run of the month, split into social security tax (the 1% band) and income tax. Deposit with the tax office within 25 days of month end. The CSV includes full PANs.'
          : 'SSF, provident fund, gratuity and CIT for every locked run of the month, to deposit with each fund (SSF within 25 days of month end).'}
      </Text>
      {q.error ? (
        <Alert color="red" variant="light">
          {q.error}
        </Alert>
      ) : (
        <ReportTable result={q.data} empty={`No locked payslips for ${bsMonthLabel(m.year, m.month)}`} />
      )}
    </Stack>
  );
}

function VarianceReport() {
  const runs = useQuery(() => payApi.runs(), []);
  const regular = (runs.data?.runs ?? []).filter((r) => r.run_type === 'regular' && r.computed_at);
  const [runId, setRunId] = useState(null);
  const id = runId ?? regular[0]?.id ?? null;
  const q = useQuery(() => (id ? compliance.variance(id) : Promise.resolve(null)), [id]);
  const label = (r) =>
    `${r.bs_year && r.bs_month ? bsMonthLabel(r.bs_year, r.bs_month) : r.created_at.slice(0, 10)}${r.locked_at ? '' : ' (not locked)'}`;
  return (
    <Stack>
      <Group justify="space-between" align="flex-end" wrap="wrap">
        <Select
          label="Pay run"
          value={id}
          onChange={setRunId}
          data={regular.map((r) => ({ value: r.id, label: label(r) }))}
          w={260}
          allowDeselect={false}
        />
        {id ? (
          <Button
            variant="default"
            leftSection={<IconDownload {...ICON} />}
            onClick={() => compliance.varianceCsv(id).catch((e) => toast.err(e))}
          >
            Download CSV
          </Button>
        ) : null}
      </Group>
      <Text size="sm" c="dimmed">
        {q.data?.previous
          ? `Each person's net pay against ${bsMonthLabel(q.data.previous.bsYear, q.data.previous.bsMonth)}, biggest changes first, with the likely reasons.`
          : 'No earlier locked regular run to compare with yet.'}
      </Text>
      {!regular.length ? (
        <EmptyState compact title="No regular pay runs yet" />
      ) : q.data ? (
        <ReportTable
          empty="No payslips in this run"
          result={{
            columns: [
              { key: 'empCode', header: 'Code' },
              { key: 'name', header: 'Name' },
              { key: 'previousNet', header: 'Previous net' },
              { key: 'net', header: 'Net' },
              { key: 'difference', header: 'Difference' },
              { key: 'percent', header: 'Change %' },
              { key: 'reasons', header: 'Why' },
            ],
            rows: q.data.rows,
          }}
        />
      ) : null}
    </Stack>
  );
}

function LiabilityReport() {
  const [asOf, setAsOf] = useState(todayNpt());
  const q = useQuery(() => compliance.leaveLiability(asOf), [asOf]);
  return (
    <Stack>
      <Group justify="space-between" align="flex-end" wrap="wrap">
        <DateField label="As of" value={asOf} onChange={(v) => v && setAsOf(v)} w={200} />
        <Button
          variant="default"
          leftSection={<IconDownload {...ICON} />}
          onClick={() => compliance.leaveLiabilityCsv(asOf).catch((e) => toast.err(e))}
        >
          Download CSV
        </Button>
      </Group>
      <Text size="sm" c="dimmed">
        What unused encashable leave would cost if paid out: balance × basic salary ÷ {q.data?.divisor ?? 'the encashment divisor'} days
        (Payroll settings).
      </Text>
      {q.data ? (
        <StatStrip
          stats={[
            { key: 't', label: 'Total liability', value: fmtMoney(q.data.total, { decimals: 0 }) },
            ...q.data.byDepartment
              .slice(0, 3)
              .map((d) => ({ key: d.department, label: d.department, value: fmtMoney(d.amount, { decimals: 0 }) })),
          ]}
        />
      ) : null}
      {q.data ? (
        <ReportTable
          empty="No encashable leave balances"
          result={{
            columns: [
              { key: 'empCode', header: 'Code' },
              { key: 'name', header: 'Name' },
              { key: 'department', header: 'Department' },
              { key: 'leaveType', header: 'Leave type' },
              { key: 'balance', header: 'Days' },
              { key: 'dailyRate', header: 'Daily rate' },
              { key: 'amount', header: 'Amount' },
            ],
            rows: q.data.rows,
          }}
        />
      ) : null}
    </Stack>
  );
}

function AnnualReport({ fy, setFy, fyOptions }) {
  const q = useQuery(() => compliance.annualTax(fy), [fy]);
  return (
    <Stack>
      <Group justify="space-between" align="flex-end" wrap="wrap">
        <Select
          label="Fiscal year"
          value={String(fy)}
          onChange={(v) => v && setFy(Number(v))}
          data={fyOptions}
          allowDeselect={false}
          w={160}
        />
        <Button
          variant="default"
          leftSection={<IconDownload {...ICON} />}
          onClick={() => compliance.annualTaxCsv(fy).catch((e) => toast.err(e))}
        >
          Download CSV
        </Button>
      </Group>
      <Text size="sm" c="dimmed">
        Remuneration, retirement contributions, tax liability and tax deducted for everyone paid in the year, including opening balances.
        Until Ashadh is paid, the liability is the projection from the latest payslip.
      </Text>
      <ReportTable result={q.data} empty="No locked payslips in this fiscal year" />
    </Stack>
  );
}

function Certificates({ fy, setFy, fyOptions }) {
  const [emp, setEmp] = useState(null);
  const [purpose, setPurpose] = useState('');
  const open = (kind) => emp && window.open(compliance.certificateUrl(emp, kind, fy, purpose.trim() || undefined), '_blank', 'noopener');
  return (
    <Paper withBorder p="lg">
      <SimpleGrid cols={{ base: 1, sm: 3 }}>
        <EmployeePicker value={emp} onChange={setEmp} status="all" />
        <Select label="Fiscal year" value={String(fy)} onChange={(v) => v && setFy(Number(v))} data={fyOptions} allowDeselect={false} />
        <TextInput
          label="Purpose (salary certificate)"
          value={purpose}
          onChange={(e) => setPurpose(e.currentTarget.value)}
          placeholder="e.g. Bank loan"
          maxLength={120}
        />
      </SimpleGrid>
      <Group mt="md" gap="xs">
        <Button leftSection={<IconFileCertificate {...ICON} />} disabled={!emp} onClick={() => open('tax')}>
          Tax certificate
        </Button>
        <Button variant="default" leftSection={<IconFileCertificate {...ICON} />} disabled={!emp} onClick={() => open('salary')}>
          Salary certificate
        </Button>
      </Group>
      <Text size="xs" c="dimmed" mt="sm">
        Opens a printable page. Employees can open their own certificates from Payslips. Every certificate HR opens is recorded in the audit
        log.
      </Text>
    </Paper>
  );
}

/** HR: statutory and payroll reports (no bank sheet or accounting journal; HR runs payroll alone). */
export default function PayrollReportsPage() {
  const [kind, setKind] = useState('tax');
  const bs = safeAdToBs(todayNpt());
  const curFy = bs ? (bs.month >= 4 ? bs.year : bs.year - 1) : 2083;
  const [fy, setFy] = useState(curFy);
  const fyOptions = [curFy - 2, curFy - 1, curFy].map((y) => ({ value: String(y), label: `FY ${y}/${String(y + 1).slice(-2)}` }));
  return (
    <Stack gap="lg">
      <PageHeader
        title="Payroll reports"
        subtitle="Monthly deposits, changes between months, leave liability and the annual tax picture. Figures come from locked pay runs only."
      />
      <ScrollArea type="auto" offsetScrollbars>
        <SegmentedControl
          value={kind}
          onChange={(v) => setKind(v)}
          data={[
            { value: 'tax', label: 'Tax deposit' },
            { value: 'funds', label: 'Retirement funds' },
            { value: 'variance', label: 'Pay variance' },
            { value: 'liability', label: 'Leave liability' },
            { value: 'annual', label: 'Annual tax' },
            { value: 'certs', label: 'Certificates' },
          ]}
        />
      </ScrollArea>
      {kind === 'tax' || kind === 'funds' ? <MonthReport key={kind} kind={kind} /> : null}
      {kind === 'variance' ? <VarianceReport /> : null}
      {kind === 'liability' ? <LiabilityReport /> : null}
      {kind === 'annual' ? <AnnualReport fy={fy} setFy={setFy} fyOptions={fyOptions} /> : null}
      {kind === 'certs' ? <Certificates fy={fy} setFy={setFy} fyOptions={fyOptions} /> : null}
      <Alert variant="light" color="gray" icon={<IconInfoCircle size={18} />}>
        Tax rules (bands, deduction limits, the medical credit and the donation deduction) follow the versions under Tax & SSF rules.
        Confirm them with your accountant before filing.
      </Alert>
    </Stack>
  );
}
