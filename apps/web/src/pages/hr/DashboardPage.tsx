import { Badge, Group, Paper, Stack, Text } from '@mantine/core';
import { Link } from 'react-router-dom';
import { dashboard as dashApi } from '../../api';
import { KpiTiles, TrendBars } from '../../components/Kpi';
import { PageHeader } from '../../components/PageHeader';
import { useI18n } from '../../i18n';
import { bsMonthLabel, dualDate, fmtMoney, minutesToHm } from '../../lib/format';
import { useQuery } from '../../lib/useQuery';

const LINK: Record<string, string> = { device_offline: '/hr/devices', unmapped_punches: '/hr/devices', ot_unapproved: '/hr/ot-slips', leave_overdue: '/manager/approvals' };

export default function DashboardPage() {
  const { t, lang, numerals } = useI18n();
  const q = useQuery(() => dashApi.executive(), []);
  const d = q.data;
  return (
    <Stack>
      <PageHeader title={t('dashboard')} subtitle={d ? `${dualDate(d.date, lang, numerals).bsLong} · ${dualDate(d.date).ad}` : undefined} />
      {d ? <KpiTiles tiles={d.tiles} /> : null}
      {d ? <TrendBars label={t('trend30')} points={d.trend.map((p) => ({ date: p.date, value: p.presentPct }))} /> : null}
      <Paper withBorder p="md" radius="md">
        <Text fw={600} mb="xs">{t('needsAttention')}</Text>
        {d && d.needsAttention.length === 0 ? <Text size="sm" c="dimmed">Nothing needs attention right now.</Text> : null}
        <Stack gap={6}>{(d?.needsAttention ?? []).map((n, i) => <Group key={i} justify="space-between"><Text size="sm" component={Link} to={LINK[n.kind] ?? '/hr/dashboard'}>{n.label}</Text><Badge variant="light" color="orange">{n.kind.replace('_', ' ')}</Badge></Group>)}</Stack>
      </Paper>
      {d ? <Group gap="xl" wrap="wrap"><Text size="sm">{t('overtime')} this month: <strong>{minutesToHm(d.otMinutesThisMonth, numerals)}</strong></Text>
        {d.lastPayrollRun ? <Text size="sm">Last payroll: <strong>{bsMonthLabel(d.lastPayrollRun.bs_year, d.lastPayrollRun.bs_month, lang, numerals)}</strong> · {d.lastPayrollRun.status} · {d.lastPayrollRun.headcount} payslips · net {fmtMoney(d.lastPayrollRun.net, { numerals })}</Text> : <Text size="sm" c="dimmed">No payroll run yet.</Text>}</Group> : null}
    </Stack>
  );
}
