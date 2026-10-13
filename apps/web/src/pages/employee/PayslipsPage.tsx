import { Select, Stack } from '@mantine/core';
import { useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { payroll as payApi } from '../../api';
import type { PayslipSummary } from '../../api/types';
import { useAuth } from '../../auth/AuthProvider';
import { DataTable } from '../../components/DataTable';
import { PageHeader } from '../../components/PageHeader';
import { useI18n } from '../../i18n';
import { bsMonthLabel, fmtMoney, fmtNum, safeAdToBs, todayNpt } from '../../lib/format';
import { useQuery } from '../../lib/useQuery';

export default function PayslipsPage() {
  const { user } = useAuth();
  const { t, lang, numerals } = useI18n();
  const nav = useNavigate();
  const [params] = useSearchParams();
  const employeeId = params.get('employeeId') ?? user?.employeeId ?? '';
  const bs = safeAdToBs(todayNpt());
  const curFy = bs ? (bs.month >= 4 ? bs.year : bs.year - 1) : 2083;
  const [fy, setFy] = useState<string>(String(curFy));
  const q = useQuery(() => payApi.payslips({ employeeId, fiscalYearStart: Number(fy) }), [employeeId, fy], { enabled: !!employeeId });
  const years = [curFy - 2, curFy - 1, curFy].map((y) => ({ value: String(y), label: `${y}/${String(y + 1).slice(-2)}` }));
  return (
    <Stack>
      <PageHeader title={t('payslips')} actions={<Select label={t('fiscalYear')} value={fy} onChange={(v) => v && setFy(v)} data={years} allowDeselect={false} w={140} />} />
      <DataTable<PayslipSummary> rows={q.data?.payslips ?? []} rowKey={(r) => r.id} loading={q.loading} error={q.error} emptyTitle={lang === 'ne' ? 'यो आर्थिक वर्षमा तलब विवरण छैन।' : 'No payslips for this fiscal year yet.'} emptyHint={lang === 'ne' ? 'तलब प्रकाशन भएपछि यहाँ देखिन्छ।' : 'Payslips appear here once payroll is published.'}
        onRowClick={(r) => nav(`/me/payslips/${r.id}`)}
        columns={[
          { key: 'period', header: t('month'), render: (r) => `${bsMonthLabel(r.bs_year ?? 0, r.bs_month ?? 1, lang, numerals)}${r.run_type && r.run_type !== 'regular' ? ` (${r.run_type})` : ''}` },
          { key: 'paid', header: 'Paid days', render: (r) => `${fmtNum(r.paid_days, numerals)}${Number(r.lop_days) > 0 ? ` (−${r.lop_days})` : ''}`, align: 'right' },
          { key: 'gross', header: t('grossPay'), render: (r) => fmtMoney(r.gross, { numerals }), align: 'right' },
          { key: 'ded', header: t('deductions'), render: (r) => fmtMoney(r.total_deductions, { numerals }), align: 'right' },
          { key: 'net', header: t('netPay'), render: (r) => <strong>{fmtMoney(r.net, { numerals })}</strong>, align: 'right' },
        ]} />
    </Stack>
  );
}
