import { Badge, Button, Group, Paper, ScrollArea, Table, Text } from '@mantine/core';
import { IconDownload, IconPrinter, IconTable } from '@tabler/icons-react';
import { payroll as payApi } from '../../api';
import type { SalarySheet as Sheet } from '../../api/types';
import { EmptyState } from '../../components/EmptyState';
import { StatStrip } from '../../components/Kpi';
import { SectionTitle } from '../../components/PageHeader';
import { fmtMoney } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const ICON = { size: 16, stroke: 1.6 } as const;
const m = (x: string | undefined) => fmtMoney(x ?? '0', { currency: false });
const signed = (x: string) => (Number(x) === 0 ? '' : `${Number(x) > 0 ? '+' : '−'}${fmtMoney(String(Math.abs(Number(x))), { currency: false })}`);
const sticky = { position: 'sticky', left: 0, background: 'var(--mantine-color-body)', zIndex: 1 } as const;
const groupHead = (label: string, span: number, tone?: string) => span > 0 ? <Table.Th colSpan={span} ta="center" style={{ fontSize: 11, color: tone ?? 'var(--hj-muted)', borderBottom: '1px solid var(--hj-line)' }}>{label}</Table.Th> : null;

/** One table of everyone's pay for the run, with the Excel download and print view HR hands on for payment. */
export function SalarySheet({ runId, version }: { runId: string; version: string }) {
  const q = useQuery(() => payApi.salarySheet(runId), [runId, version]);
  const s: Sheet | null | undefined = q.data;
  if (q.error) return <EmptyState compact title="Salary sheet unavailable" hint={String(q.error)} />;
  if (!s) return null;
  const { earnings, deductions, employer, rounding } = s.columns;
  const filename = `salary-sheet-${s.period.bsYear}-${String(s.period.bsMonth).padStart(2, '0')}${s.run.final ? '' : '-draft'}.csv`;
  const t = s.totals;
  return (
    <>
      <SectionTitle hint={s.run.final ? 'Final. The run is locked, so these figures will not change. Download it for payment or print it for signatures.' : 'Draft. Figures can still change until the run is approved and locked.'}
        actions={<Group gap="xs">
          {!s.run.final ? <Badge variant="light" color="yellow">Draft</Badge> : null}
          <Button variant="default" leftSection={<IconDownload {...ICON} />} disabled={!s.rows.length} onClick={() => payApi.salarySheetCsv(runId, filename).catch((e) => toast.err(e))}>Download for Excel</Button>
          <Button variant="default" leftSection={<IconPrinter {...ICON} />} disabled={!s.rows.length} onClick={() => window.open(`/api/v1/payroll/runs/${runId}/salary-sheet?format=html`, '_blank', 'noopener')}>Print</Button>
        </Group>}>Salary sheet, {s.period.label}</SectionTitle>
      <StatStrip stats={[
        { key: 'n', label: 'Employees', value: String(s.rows.length) },
        { key: 'gross', label: 'Gross pay', value: fmtMoney(t.gross) },
        { key: 'ded', label: 'Deductions', value: fmtMoney(t.totalDeductions) },
        { key: 'net', label: 'Net pay', value: fmtMoney(t.net) },
        { key: 'ctc', label: 'Cost to company', value: fmtMoney(t.costToCompany) },
      ]} />
      {!s.rows.length ? <EmptyState compact icon={<IconTable size={20} stroke={1.6} />} title="No payslips yet" hint="Calculate the run and the salary sheet fills in here." /> : (
        <Paper withBorder style={{ overflow: 'hidden' }}>
          <ScrollArea type="auto">
            <Table stickyHeader withColumnBorders={false} miw={900} style={{ whiteSpace: 'nowrap' }}>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th style={sticky} />
                  <Table.Th colSpan={2} />
                  {groupHead('Earnings', earnings.length + 1, 'var(--hj-ok)')}
                  {groupHead('Deductions', deductions.length + 1, 'var(--hj-bad)')}
                  <Table.Th colSpan={rounding ? 2 : 1} />
                  {groupHead('Employer', employer.length + 2)}
                </Table.Tr>
                <Table.Tr>
                  <Table.Th style={sticky}>Employee</Table.Th>
                  <Table.Th ta="right">Paid days</Table.Th>
                  <Table.Th ta="right">Unpaid</Table.Th>
                  {earnings.map((c) => <Table.Th key={c.code} ta="right">{c.name}</Table.Th>)}
                  <Table.Th ta="right">Gross</Table.Th>
                  {deductions.map((c) => <Table.Th key={c.code} ta="right">{c.name}</Table.Th>)}
                  <Table.Th ta="right">Total</Table.Th>
                  {rounding ? <Table.Th ta="right">Rounding</Table.Th> : null}
                  <Table.Th ta="right">Net pay</Table.Th>
                  {employer.map((c) => <Table.Th key={c.code} ta="right">{c.name}</Table.Th>)}
                  <Table.Th ta="right">Total</Table.Th>
                  <Table.Th ta="right">Cost to company</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {s.rows.map((r) => (
                  <Table.Tr key={r.employeeId}>
                    <Table.Td style={sticky}><Text size="sm" fw={500}>{r.name}</Text><Text size="xs" c="dimmed">{r.empCode}{r.designation ? ` · ${r.designation}` : ''}</Text></Table.Td>
                    <Table.Td ta="right" className="num">{r.paidDays}</Table.Td>
                    <Table.Td ta="right" className="num" c={Number(r.lopDays) ? 'orange' : 'dimmed'}>{r.lopDays}</Table.Td>
                    {earnings.map((c) => <Table.Td key={c.code} ta="right" className="num">{r.earnings[c.code] ? m(r.earnings[c.code]) : <Text span c="dimmed">–</Text>}</Table.Td>)}
                    <Table.Td ta="right" className="num" fw={600}>{m(r.gross)}</Table.Td>
                    {deductions.map((c) => <Table.Td key={c.code} ta="right" className="num">{r.deductions[c.code] ? m(r.deductions[c.code]) : <Text span c="dimmed">–</Text>}</Table.Td>)}
                    <Table.Td ta="right" className="num" fw={600}>{m(r.totalDeductions)}</Table.Td>
                    {rounding ? <Table.Td ta="right" className="num" c="dimmed">{signed(r.rounding)}</Table.Td> : null}
                    <Table.Td ta="right" className="num" fw={700} c="brand">{m(r.net)}{r.flags?.includes('PayHeld') ? <Text size="xs" c="orange" fw={500}>On hold</Text> : null}</Table.Td>
                    {employer.map((c) => <Table.Td key={c.code} ta="right" className="num">{r.employer[c.code] ? m(r.employer[c.code]) : <Text span c="dimmed">–</Text>}</Table.Td>)}
                    <Table.Td ta="right" className="num">{m(r.employerTotal)}</Table.Td>
                    <Table.Td ta="right" className="num">{m(r.costToCompany)}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
              <Table.Tfoot style={{ borderTop: '1px solid var(--hj-line)' }}>
                <Table.Tr>
                  <Table.Th style={sticky}>Total ({s.rows.length})</Table.Th>
                  <Table.Th ta="right" className="num">{t.paidDays}</Table.Th>
                  <Table.Th ta="right" className="num">{t.lopDays}</Table.Th>
                  {earnings.map((c) => <Table.Th key={c.code} ta="right" className="num">{m(t.earnings[c.code])}</Table.Th>)}
                  <Table.Th ta="right" className="num">{m(t.gross)}</Table.Th>
                  {deductions.map((c) => <Table.Th key={c.code} ta="right" className="num">{m(t.deductions[c.code])}</Table.Th>)}
                  <Table.Th ta="right" className="num">{m(t.totalDeductions)}</Table.Th>
                  {rounding ? <Table.Th ta="right" className="num">{signed(t.rounding)}</Table.Th> : null}
                  <Table.Th ta="right" className="num" c="brand">{m(t.net)}</Table.Th>
                  {employer.map((c) => <Table.Th key={c.code} ta="right" className="num">{m(t.employer[c.code])}</Table.Th>)}
                  <Table.Th ta="right" className="num">{m(t.employerTotal)}</Table.Th>
                  <Table.Th ta="right" className="num">{m(t.costToCompany)}</Table.Th>
                </Table.Tr>
              </Table.Tfoot>
            </Table>
          </ScrollArea>
        </Paper>
      )}
    </>
  );
}
