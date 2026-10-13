import { Select, Stack, Text } from '@mantine/core';
import { useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { payroll as payApi } from '../../api';
import type { PayslipSummary } from '../../api/types';
import { useAuth } from '../../auth/AuthProvider';
import { DataTable } from '../../components/DataTable';
import { StatStrip } from '../../components/Kpi';
import { PageHeader } from '../../components/PageHeader';
import { bsMonthLabel, fmtMoney, safeAdToBs, todayNpt } from '../../lib/format';
import { useQuery } from '../../lib/useQuery';

const runTypeLabel = (s: string) => s.charAt(0).toUpperCase() + s.slice(1).replace(/_/g, ' ') + ' run';

export default function PayslipsPage() {
  const { user } = useAuth();
  const nav = useNavigate();
  const [params] = useSearchParams();
  const employeeId = params.get('employeeId') ?? user?.employeeId ?? '';
  const bs = safeAdToBs(todayNpt());
  const curFy = bs ? (bs.month >= 4 ? bs.year : bs.year - 1) : 2083;
  const [fy, setFy] = useState<string>(String(curFy));
  const q = useQuery(() => payApi.payslips({ employeeId, fiscalYearStart: Number(fy) }), [employeeId, fy], { enabled: !!employeeId });
  const years = [curFy - 2, curFy - 1, curFy].map((y) => ({ value: String(y), label: `FY ${y}/${String(y + 1).slice(-2)}` }));
  const rows = q.data?.payslips ?? [];
  const sum = (k: 'gross' | 'net' | 'tds') => rows.reduce((a, r) => a + Number(r[k] ?? 0), 0);
  return (
    <Stack gap="lg">
      <PageHeader title="Payslips" subtitle="Payslips appear here once payroll is published for the month."
        actions={<Select aria-label="Fiscal year" value={fy} onChange={(v) => v && setFy(v)} data={years} allowDeselect={false} w={150} />} />
      {rows.length ? (
        <StatStrip stats={[
          { key: 'net', label: 'Net pay this year', value: fmtMoney(sum('net'), { decimals: 0 }) },
          { key: 'gross', label: 'Gross pay this year', value: fmtMoney(sum('gross'), { decimals: 0 }) },
          { key: 'tds', label: 'Tax deducted', value: fmtMoney(sum('tds'), { decimals: 0 }) },
          { key: 'count', label: 'Payslips', value: rows.length },
        ]} />
      ) : null}
      <DataTable<PayslipSummary> rows={rows} rowKey={(r) => r.id} loading={q.loading} error={q.error}
        emptyTitle="No payslips for this fiscal year yet" emptyHint="They appear here once payroll is published. Try an earlier fiscal year if you're looking for older ones."
        onRowClick={(r) => nav(`/me/payslips/${r.id}`)}
        columns={[
          { key: 'period', header: 'Month', render: (r) => (
            <div>
              <Text size="sm" fw={500}>{bsMonthLabel(r.bs_year ?? 0, r.bs_month ?? 1)}</Text>
              {r.run_type && r.run_type !== 'regular' ? <Text size="xs" c="dimmed">{runTypeLabel(r.run_type)}</Text> : null}
            </div>
          ) },
          { key: 'paid', header: 'Paid days', align: 'right', render: (r) => <><span className="num">{Number(r.paid_days)}</span>{Number(r.lop_days) > 0 ? <Text span size="xs" c="dimmed" className="num">{` (${Number(r.lop_days)} unpaid)`}</Text> : null}</> },
          { key: 'gross', header: 'Gross', align: 'right', render: (r) => <span className="num">{fmtMoney(r.gross)}</span> },
          { key: 'ded', header: 'Deductions', align: 'right', render: (r) => <span className="num">{fmtMoney(r.total_deductions)}</span> },
          { key: 'net', header: 'Net pay', align: 'right', render: (r) => <Text span size="sm" fw={600} className="num">{fmtMoney(r.net)}</Text> },
        ]} />
    </Stack>
  );
}
